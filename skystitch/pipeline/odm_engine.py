"""
odm_engine.py
=============
Full-photogrammetry engine for SkyStitch: instead of the lightweight 2D
homography stitch in ``core.py``, this drives **OpenDroneMap (ODM)** to run
a real Structure-from-Motion + Multi-View-Stereo pipeline and produce the
same family of outputs as a commercial photogrammetry suite:

    * orthophoto (true, relief-corrected)   -> odm_orthophoto/odm_orthophoto.tif
    * DSM  (digital surface model)          -> odm_dem/dsm.tif
    * DTM  (digital terrain model / DEM)    -> odm_dem/dtm.tif
    * point cloud (LAZ)                     -> odm_georeferencing/odm_georeferenced_model.laz
    * textured 3D mesh (OBJ)                -> odm_texturing/odm_textured_model_geo.obj

ODM itself is run through its official Docker image (``opendronemap/odm``),
so the only host requirement is a working Docker install -- no compiling of
OpenSfM/OpenMVS. This module builds the ``docker run`` command, streams its
log output back through a ``feedback`` callback, maps ODM's processing
stages onto a 0-100 progress value, supports cancellation (by killing the
container), and finally returns the paths of the products it found.

Design deliberately mirrors ``core.run_pipeline``: same ``feedback`` /
``progress`` / ``is_canceled`` callback trio, same "can also be run as a
CLI" property, so ``worker.py``-style background tasks can wrap either
engine the same way.
"""

import logging
import os
import shutil
import subprocess
import sys
import uuid
import platform

# Reuse the exact same photo-discovery logic the 2D engine uses, so a folder
# that "sees" N photos in the SkyStitch dialog feeds the same N to ODM.
try:
    from .core import find_photos
except Exception:  # pragma: no cover - allow standalone/CLI import
    import glob

    def find_photos(input_dir, pattern):
        out = []
        for pat in str(pattern).split(";"):
            pat = pat.strip()
            if pat:
                out.extend(glob.glob(os.path.join(input_dir, pat)))
        # de-duplicate case-insensitively while keeping order
        seen, uniq = set(), []
        for p in sorted(out):
            k = os.path.normcase(os.path.abspath(p))
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        return uniq


DEFAULT_IMAGE = "opendronemap/odm"
GPU_IMAGE = "opendronemap/odm:gpu"
DEFAULT_NAME = "skystitch_odm"
DEFAULT_PATTERN = "*.jpg;*.JPG;*.jpeg;*.JPEG"

# ODM runs a fixed, ordered list of stages. We can't get a clean percentage
# out of ODM, but every stage transition is logged as "running <stage> stage",
# so mapping the stage name to its index in this list gives a good-enough
# progress bar and a human-readable "what's happening now" label.
_STAGES = [
    ("dataset", "Loading dataset"),
    ("split", "Splitting (large datasets)"),
    ("merge", "Merging (large datasets)"),
    ("opensfm", "Structure-from-Motion (camera positions)"),
    ("openmvs", "Dense point cloud (Multi-View Stereo)"),
    ("odm_filterpoints", "Filtering point cloud"),
    ("odm_meshing", "Building 3D mesh"),
    ("mvs_texturing", "Texturing 3D model"),
    ("odm_georeferencing", "Georeferencing"),
    ("odm_dem", "Building DSM / DTM"),
    ("odm_orthophoto", "Building orthophoto"),
    ("odm_report", "Writing report"),
    ("odm_postprocess", "Post-processing"),
]
_STAGE_TOTAL = len(_STAGES)


class OdmError(Exception):
    """A problem that stopped ODM from finishing (bad input, Docker missing,
    non-zero exit, missing outputs, ...)."""


class OdmCanceled(Exception):
    """Raised when the user canceled the run."""


def _noop_feedback(msg):
    pass


def _noop_progress(pct):
    pass


def _noop_is_canceled():
    return False


def _docker_path(p):
    """Return an absolute path in a form the Docker CLI accepts as a bind-mount
    source. On Windows, Docker Desktop is happy with forward slashes and the
    ``C:/Users/...`` drive form, so normalise backslashes away."""
    ap = os.path.abspath(p)
    if os.name == "nt":
        ap = ap.replace("\\", "/")
    return ap


def _check_docker(docker_exe, feedback):
    """Fail early (before copying photos) with a clear, actionable message if
    Docker isn't callable."""
    try:
        out = subprocess.run(  # nosec B603 - fixed argv, no shell; docker path is user-configured
            [docker_exe, "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except FileNotFoundError:
        raise OdmError(
            "Docker was not found on this system. The OpenDroneMap (3D) engine "
            "runs ODM through Docker.\n"
            "Install Docker Desktop (Windows/macOS) or the docker engine (Linux), "
            "make sure the 'docker' command works in a terminal, then try again.\n"
            "See README.md -> 'OpenDroneMap (3D) mode' for details."
        )
    if out.returncode != 0:
        raise OdmError(
            "Docker is installed but not responding (is Docker Desktop running?).\n"
            f"'{docker_exe} --version' exited with code {out.returncode}:\n{out.stdout}"
        )
    feedback(f"[INFO] {out.stdout.strip()}")


def docker_status(docker_exe="docker"):
    """Probe Docker for the 'Test Docker' button. Returns a dict: found (the
    docker CLI is callable), daemon (the engine responds), version, message."""
    info = {"found": False, "daemon": False, "version": "", "message": ""}
    try:
        v = subprocess.run([docker_exe, "--version"], stdout=subprocess.PIPE,  # nosec B603
                           stderr=subprocess.STDOUT, text=True, timeout=15)
    except FileNotFoundError:
        info["message"] = ("Docker command '%s' was not found on PATH. Install Docker, or set "
                           "the full path to docker.exe in the 'Docker command' field." % docker_exe)
        return info
    except Exception as e:
        info["message"] = "Could not run '%s': %s" % (docker_exe, e)
        return info
    if v.returncode != 0:
        info["message"] = "'%s --version' failed: %s" % (docker_exe, (v.stdout or "").strip())
        return info
    info["found"] = True
    info["version"] = (v.stdout or "").strip()
    try:
        d = subprocess.run([docker_exe, "info"], stdout=subprocess.PIPE,  # nosec B603
                           stderr=subprocess.STDOUT, text=True, timeout=20)
        if d.returncode == 0:
            info["daemon"] = True
            info["message"] = "OK - %s, and the Docker engine is running." % info["version"]
        else:
            info["message"] = ("%s found, but the Docker engine is not responding (start Docker "
                               "Desktop and wait until it says 'running')." % info["version"])
    except subprocess.TimeoutExpired:
        info["message"] = ("%s found, but the Docker engine did not respond in time (is Docker "
                           "Desktop still starting?)." % info["version"])
    except Exception as e:
        info["message"] = "%s found, but checking the engine failed: %s" % (info["version"], e)
    return info


def docker_install_hint():
    """Return (method, command_list_or_None, url) for installing Docker on this OS."""
    sysname = platform.system()
    if sysname == "Windows":
        return ("winget",
                ["winget", "install", "-e", "--id", "Docker.DockerDesktop",
                 "--accept-package-agreements", "--accept-source-agreements"],
                "https://www.docker.com/products/docker-desktop/")
    if sysname == "Darwin":
        return ("web", None, "https://www.docker.com/products/docker-desktop/")
    return ("web", None, "https://docs.docker.com/engine/install/")


def start_docker_install(feedback=_noop_feedback):
    """Best-effort launch of a Docker install for the 'Install Docker' button. On
    Windows, runs winget in a new console (UAC prompts). Elsewhere, or if winget
    is missing, returns ('open_url', url) so the dialog opens the page.
    Returns ('started', None) or ('open_url', url)."""
    feedback = feedback or _noop_feedback
    method, cmd, url = docker_install_hint()
    if method == "winget":
        try:
            subprocess.run(["winget", "--version"], stdout=subprocess.DEVNULL,  # nosec B603 B607
                           stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            feedback("[INFO] winget not available; opening the Docker download page instead.")
            return ("open_url", url)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        try:
            subprocess.Popen(cmd, creationflags=flags)  # nosec B603 - fixed winget argv
            feedback("[INFO] Launched 'winget install Docker.DockerDesktop' in a new window. "
                     "Approve the UAC prompt, let it finish, then RESTART Windows and QGIS.")
            return ("started", None)
        except Exception as e:
            feedback("[WARNING] Could not launch winget (%s); opening the download page instead." % e)
            return ("open_url", url)
    return ("open_url", url)


def _prepare_project(input_dir, project_dir, name, pattern, gcp_path, feedback, is_canceled):
    """Lay out the ``<project_dir>/<name>/images`` structure ODM expects and
    populate it. Photos are hard-linked when possible (instant, no extra disk),
    falling back to a copy across filesystems/drives."""
    photos = find_photos(input_dir, pattern)
    if len(photos) < 2:
        raise OdmError(
            f"Found {len(photos)} photo(s) matching '{pattern}'. OpenDroneMap needs "
            "several overlapping photos (typically 20+); for 1-2 photos use the 2D "
            "SkyStitch engine instead."
        )

    images_dir = os.path.join(project_dir, name, "images")
    os.makedirs(images_dir, exist_ok=True)

    feedback(f"[INFO] Staging {len(photos)} photo(s) into the ODM project folder...")
    linked = copied = 0
    for i, src in enumerate(photos):
        if is_canceled():
            raise OdmCanceled()
        dst = os.path.join(images_dir, os.path.basename(src))
        if os.path.exists(dst):
            continue
        try:
            os.link(src, dst)          # hard link: no extra disk, instant
            linked += 1
        except OSError:
            shutil.copy2(src, dst)     # different drive / FS without hardlinks
            copied += 1
    feedback(f"[INFO] Staged photos ({linked} linked, {copied} copied).")

    if gcp_path:
        if not os.path.isfile(gcp_path):
            raise OdmError(f"GCP file not found: {gcp_path}")
        # ODM auto-detects a file literally named gcp_list.txt in the project dir.
        dst_gcp = os.path.join(project_dir, name, "gcp_list.txt")
        shutil.copy2(gcp_path, dst_gcp)
        feedback(f"[INFO] GCP file staged as {dst_gcp} (will be used for georeferencing).")

    return images_dir, len(photos)


def _build_command(docker_exe, image, mount, name, opts):
    cmd = [
        docker_exe, "run", "-i", "--rm",
        "--name", opts["container_name"],
        "-v", f"{mount}:/datasets",
        image,
        "--project-path", "/datasets", name,
    ]
    if opts["produce_dsm"]:
        cmd.append("--dsm")
    if opts["produce_dtm"]:
        cmd.append("--dtm")
    if not opts["produce_mesh"]:
        cmd.append("--skip-3dmodel")     # saves a lot of time if only 2.5D products are needed
    if opts["fast_orthophoto"]:
        cmd.append("--fast-orthophoto")  # skips full dense cloud; quick ortho for flat scenes
    if opts["pc_quality"]:
        cmd += ["--pc-quality", opts["pc_quality"]]
    if opts["ortho_resolution_cm"]:
        cmd += ["--orthophoto-resolution", str(opts["ortho_resolution_cm"])]
    if opts["dem_resolution_cm"]:
        cmd += ["--dem-resolution", str(opts["dem_resolution_cm"])]
    if opts["max_concurrency"]:
        cmd += ["--max-concurrency", str(opts["max_concurrency"])]
    if opts["gcp_path"]:
        cmd += ["--gcp", "gcp_list.txt"]
    if opts["extra_args"]:
        cmd += list(opts["extra_args"])
    return cmd


def _detect_stage(line):
    """Return (stage_index, label) if a line marks an ODM stage transition."""
    low = line.lower()
    if "running" in low and "stage" in low:
        for i, (key, label) in enumerate(_STAGES):
            if key in low:
                return i, label
    return None


def run_odm(
    input_dir,
    project_dir,
    name=DEFAULT_NAME,
    pattern=DEFAULT_PATTERN,
    produce_dsm=True,
    produce_dtm=True,
    produce_mesh=True,
    pc_quality="medium",
    ortho_resolution_cm=5.0,
    dem_resolution_cm=None,
    fast_orthophoto=False,
    gcp_path=None,
    use_gpu=False,
    docker_exe="docker",
    image=None,
    max_concurrency=None,
    extra_args=None,
    feedback=_noop_feedback,
    progress=_noop_progress,
    is_canceled=_noop_is_canceled,
):
    """Run OpenDroneMap over ``input_dir`` and return a dict of output paths.

    Returns
    -------
    dict with keys: ``ortho``, ``dsm``, ``dtm``, ``point_cloud``, ``mesh``,
    ``report``, ``project_path`` -- each a path string, or ``None`` if that
    product was not produced/requested.
    """
    feedback = feedback or _noop_feedback
    progress = progress or _noop_progress
    is_canceled = is_canceled or _noop_is_canceled

    image = image or (GPU_IMAGE if use_gpu else DEFAULT_IMAGE)
    container_name = "skystitch_odm_" + uuid.uuid4().hex[:10]

    feedback("[STAGE 0/%d] Preparing" % _STAGE_TOTAL)
    _check_docker(docker_exe, feedback)

    project_dir = os.path.abspath(project_dir)
    os.makedirs(project_dir, exist_ok=True)
    _prepare_project(input_dir, project_dir, name, pattern, gcp_path, feedback, is_canceled)
    if is_canceled():
        raise OdmCanceled()

    opts = dict(
        container_name=container_name,
        produce_dsm=produce_dsm, produce_dtm=produce_dtm, produce_mesh=produce_mesh,
        fast_orthophoto=fast_orthophoto, pc_quality=pc_quality,
        ortho_resolution_cm=ortho_resolution_cm, dem_resolution_cm=dem_resolution_cm,
        max_concurrency=max_concurrency, gcp_path=gcp_path, extra_args=extra_args,
    )
    mount = _docker_path(project_dir)
    cmd = _build_command(docker_exe, image, mount, name, opts)

    feedback("[INFO] Pulling ODM image if needed and starting container...")
    feedback("[INFO] Command: " + " ".join(cmd))

    try:
        proc = subprocess.Popen(  # nosec B603 - docker command built from validated options
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True,
        )
    except FileNotFoundError:
        raise OdmError(f"Could not launch '{docker_exe}'. Is Docker installed and on PATH?")

    canceled = False
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line:
                feedback(line)
            hit = _detect_stage(line)
            if hit is not None:
                idx, label = hit
                pct = int(round((idx / float(_STAGE_TOTAL)) * 100))
                progress(min(99, pct))
                feedback("[STAGE %d/%d] %s" % (idx + 1, _STAGE_TOTAL, label))
            if is_canceled():
                canceled = True
                feedback("[INFO] Cancellation requested -- stopping the ODM container...")
                try:
                    subprocess.run([docker_exe, "kill", container_name],  # nosec B603
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    logging.getLogger(__name__).debug("docker kill failed (best-effort)", exc_info=True)
                break
    finally:
        if proc.stdout:
            proc.stdout.close()

    ret = proc.wait()

    if canceled or is_canceled():
        raise OdmCanceled()
    if ret != 0:
        raise OdmError(
            f"OpenDroneMap exited with code {ret}. Check the log above for the ODM "
            "error (common causes: too little overlap, photos without GPS EXIF, or "
            "not enough RAM/disk). See README.md -> 'OpenDroneMap (3D) mode'."
        )

    progress(100)
    outputs = _collect_outputs(project_dir, name, produce_dsm, produce_dtm, produce_mesh)
    _report_outputs(outputs, feedback)
    return outputs


def _first_existing(*paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _collect_outputs(project_dir, name, produce_dsm, produce_dtm, produce_mesh):
    base = os.path.join(project_dir, name)
    ortho = _first_existing(
        os.path.join(base, "odm_orthophoto", "odm_orthophoto.tif"),
        os.path.join(base, "odm_orthophoto", "odm_orthophoto.original.tif"),
    )
    dsm = _first_existing(os.path.join(base, "odm_dem", "dsm.tif")) if produce_dsm else None
    dtm = _first_existing(os.path.join(base, "odm_dem", "dtm.tif")) if produce_dtm else None
    point_cloud = _first_existing(
        os.path.join(base, "odm_georeferencing", "odm_georeferenced_model.laz"),
        os.path.join(base, "odm_georeferencing", "odm_georeferenced_model.las"),
    )
    mesh = _first_existing(
        os.path.join(base, "odm_texturing", "odm_textured_model_geo.obj"),
        os.path.join(base, "odm_texturing", "odm_textured_model.obj"),
    ) if produce_mesh else None
    report = _first_existing(
        os.path.join(base, "odm_report", "report.pdf"),
        os.path.join(base, "odm_report", "report.json"),
    )
    return {
        "ortho": ortho, "dsm": dsm, "dtm": dtm,
        "point_cloud": point_cloud, "mesh": mesh, "report": report,
        "project_path": base,
    }


def _report_outputs(outputs, feedback):
    if not outputs.get("ortho"):
        feedback(
            "[WARNING] No orthophoto was found where expected. ODM may have finished "
            "only part of the pipeline; check the log and the project folder."
        )
    labels = [("ortho", "Orthophoto"), ("dsm", "DSM"), ("dtm", "DTM/DEM"),
              ("point_cloud", "Point cloud"), ("mesh", "3D mesh"), ("report", "Report")]
    for key, label in labels:
        if outputs.get(key):
            feedback(f"[INFO] {label}: {outputs[key]}")


# ---- can also be run as a standalone CLI, same spirit as core.py ----
def _main_cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Run OpenDroneMap (via Docker) over a folder of drone photos."
    )
    parser.add_argument("--input", required=True, help="Folder of drone photos (JPG + GPS EXIF).")
    parser.add_argument("--project-dir", required=True, help="Working/output folder for ODM.")
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--pattern", default=DEFAULT_PATTERN)
    parser.add_argument("--no-dsm", action="store_true")
    parser.add_argument("--no-dtm", action="store_true")
    parser.add_argument("--no-mesh", action="store_true")
    parser.add_argument("--fast-orthophoto", action="store_true")
    parser.add_argument("--pc-quality", default="medium",
                        choices=["ultra", "high", "medium", "low", "lowest"])
    parser.add_argument("--ortho-resolution", type=float, default=5.0, help="cm/pixel")
    parser.add_argument("--dem-resolution", type=float, default=None, help="cm/pixel")
    parser.add_argument("--gcp", default=None, help="Optional gcp_list.txt")
    parser.add_argument("--gpu", action="store_true", help="Use the opendronemap/odm:gpu image")
    parser.add_argument("--docker-exe", default="docker")
    parser.add_argument("--max-concurrency", type=int, default=None)
    args = parser.parse_args(argv)

    out = run_odm(
        args.input, args.project_dir, name=args.name, pattern=args.pattern,
        produce_dsm=not args.no_dsm, produce_dtm=not args.no_dtm,
        produce_mesh=not args.no_mesh, fast_orthophoto=args.fast_orthophoto,
        pc_quality=args.pc_quality, ortho_resolution_cm=args.ortho_resolution,
        dem_resolution_cm=args.dem_resolution, gcp_path=args.gcp, use_gpu=args.gpu,
        docker_exe=args.docker_exe, max_concurrency=args.max_concurrency,
        feedback=lambda m: print(m), progress=lambda p: None,
    )
    print("\nOutputs:")
    for k, v in out.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_main_cli())
