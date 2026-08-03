"""
nodeodm_client.py
=================
Client for the **NodeODM API** -- the processing node that OpenDroneMap /
WebODM (and the WebODM native Windows installer's "ODX" node) expose locally.
This is the "each user installs the engine once, the plugin talks to it
directly" path (Model A): no WebODM login, no Docker on the user's side beyond
having a node reachable (e.g. http://localhost:3000, or the WebODM native
node shown under Processing Nodes).

Flow (resumable upload so hundreds of photos don't sit in memory):

    GET  /info                         -> test connection
    POST /task/new/init                -> uuid   (name + options)
    POST /task/new/upload/{uuid}       -> one image per request
    POST /task/new/commit/{uuid}       -> start processing
    GET  /task/{uuid}/info             -> status + progress (0-100)
    GET  /task/{uuid}/output           -> console lines
    GET  /task/{uuid}/download/all.zip -> results, then extracted locally

NodeODM may require a ``token`` (passed as a query parameter on every call).
Only depends on ``requests``. No QGIS imports -> usable as a CLI too.
"""

import glob
import json
import logging
import os
import sys
import time
import zipfile

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

STATUS = {10: "QUEUED", 20: "RUNNING", 30: "FAILED", 40: "COMPLETED", 50: "CANCELED"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def _noop(*_a, **_k):
    pass


def _false(*_a, **_k):
    return False


class NodeODMError(Exception):
    pass


class NodeODMCanceled(Exception):
    pass


def requests_available():
    return requests is not None


def find_images(folder, exts=IMAGE_EXTS):
    out = []
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith(exts):
            out.append(os.path.join(folder, fn))
    return out


class NodeODMClient:

    def __init__(self, base_url, token=None, timeout=60):
        if requests is None:
            raise NodeODMError(
                "The Python package 'requests' is not installed in QGIS's Python. "
                "Install it once via the OSGeo4W Shell:\n"
                "    python-qgis -m pip install requests"
            )
        self.base = base_url.rstrip("/")
        self.token = token or None
        self.timeout = timeout
        self.session = requests.Session()

    # -------------------------------------------------------------- helpers
    def _url(self, path):
        return "%s/%s" % (self.base, path.lstrip("/"))

    def _params(self, extra=None):
        p = {}
        if self.token:
            p["token"] = self.token
        if extra:
            p.update(extra)
        return p

    def _check(self, r, what):
        if r.status_code >= 400:
            body = ""
            try:
                body = json.dumps(r.json())
            except Exception:
                body = (r.text or "")[:400]
            raise NodeODMError("%s failed (HTTP %s): %s" % (what, r.status_code, body))
        return r

    # -------------------------------------------------------------- API
    def info(self):
        try:
            r = self.session.get(self._url("/info"), params=self._params(), timeout=self.timeout)
        except Exception as e:
            raise NodeODMError(
                "Could not reach the NodeODM node at %s (%s). Is the engine running "
                "and the URL/port correct?" % (self.base, e))
        self._check(r, "info")
        return r.json()

    def test_connection(self):
        """Return (ok, message)."""
        try:
            i = self.info()
            ver = i.get("version", "?")
            engine = i.get("engine", i.get("engineVersion", ""))
            q = i.get("taskQueueCount", "?")
            extra = (" engine=%s" % engine) if engine else ""
            return True, "OK - NodeODM node reachable at %s (version %s%s, queue %s)." % (self.base, ver, extra, q)
        except NodeODMError as e:
            return False, str(e)
        except Exception as e:
            return False, "Connection failed: %s" % e

    def new_task_init(self, options=None, name="SkyStitch task"):
        data = {"name": name}
        if options:
            data["options"] = json.dumps(options)
        r = self.session.post(self._url("/task/new/init"), params=self._params(),
                              data=data, timeout=self.timeout)
        self._check(r, "task init")
        uuid = r.json().get("uuid")
        if not uuid:
            raise NodeODMError("Node did not return a task uuid on init.")
        return uuid

    def upload_image(self, uuid, path):
        with open(path, "rb") as fh:
            files = {"images": (os.path.basename(path), fh)}
            r = self.session.post(self._url("/task/new/upload/%s" % uuid),
                                 params=self._params(), files=files, timeout=max(self.timeout, 300))
        self._check(r, "upload %s" % os.path.basename(path))

    def commit(self, uuid):
        r = self.session.post(self._url("/task/new/commit/%s" % uuid),
                             params=self._params(), timeout=self.timeout)
        self._check(r, "commit")

    def task_info(self, uuid):
        r = self.session.get(self._url("/task/%s/info" % uuid), params=self._params(), timeout=self.timeout)
        self._check(r, "task info")
        return r.json()

    def task_output(self, uuid, line=0):
        try:
            r = self.session.get(self._url("/task/%s/output" % uuid),
                                params=self._params({"line": line}), timeout=self.timeout)
            if r.status_code >= 400:
                return []
            data = r.json()
            if isinstance(data, list):
                return data
            if isinstance(data, str):
                return data.splitlines()
        except Exception:
            logging.getLogger(__name__).debug("could not parse task output", exc_info=True)
        return []

    def cancel(self, uuid):
        try:
            self.session.post(self._url("/task/cancel"), params=self._params(),
                             data={"uuid": uuid}, timeout=self.timeout)
        except Exception:
            logging.getLogger(__name__).debug("cancel request failed (best-effort)", exc_info=True)

    @staticmethod
    def _status_code(info):
        st = info.get("status")
        if isinstance(st, dict):
            return st.get("code")
        return st

    def wait_for_task(self, uuid, feedback=_noop, progress=_noop, is_canceled=_false, poll_secs=5):
        seen = 0
        last = -1
        while True:
            if is_canceled():
                feedback("[INFO] Cancel requested -- stopping the NodeODM task...")
                self.cancel(uuid)
                raise NodeODMCanceled()
            info = self.task_info(uuid)
            for ln in self.task_output(uuid, line=seen):
                feedback(ln)
                seen += 1
            code = self._status_code(info)
            pct = int(info.get("progress") or 0)  # NodeODM progress is 0-100
            if pct != last:
                progress(min(99, pct))
                last = pct
            if code == 40:
                progress(100)
                feedback("[INFO] Task completed.")
                return info
            if code == 30:
                raise NodeODMError("Processing failed: %s" % (info.get("error") or "see node console output"))
            if code == 50:
                raise NodeODMCanceled()
            time.sleep(poll_secs)

    def download_all(self, uuid, dest_zip, feedback=_noop):
        url = self._url("/task/%s/download/all.zip" % uuid)
        r = self.session.get(url, params=self._params(), stream=True, timeout=max(self.timeout, 1200))
        self._check(r, "download all.zip")
        os.makedirs(os.path.dirname(dest_zip) or ".", exist_ok=True)
        with open(dest_zip, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
        feedback("[INFO] Downloaded results archive -> %s" % dest_zip)
        return dest_zip


def _find_one(root, *patterns):
    for pat in patterns:
        hits = glob.glob(os.path.join(root, "**", pat), recursive=True)
        if hits:
            hits.sort(key=len)  # prefer shallower / simpler names
            return hits[0]
    return None


def collect_assets(extract_dir):
    """Locate the standard ODM products inside the extracted results folder."""
    return {
        "orthophoto": _find_one(extract_dir, "odm_orthophoto.tif", "orthophoto.tif"),
        "dsm": _find_one(extract_dir, "dsm.tif"),
        "dtm": _find_one(extract_dir, "dtm.tif"),
        "point_cloud": _find_one(extract_dir, "odm_georeferenced_model.laz",
                                 "*georeferenced*.laz", "*.laz", "*.las"),
        "mesh": _find_one(extract_dir, "odm_textured_model_geo.obj", "odm_textured_model.obj", "*.obj"),
        "report": _find_one(extract_dir, "report.pdf"),
    }


def process_folder(
    base_url, input_dir, output_dir, token=None,
    produce_dsm=True, produce_dtm=True, produce_mesh=True,
    pc_quality="medium", ortho_resolution_cm=5.0, dem_resolution_cm=None,
    fast_orthophoto=False, gcp_path=None, name="SkyStitch",
    feedback=_noop, progress=_noop, is_canceled=_false,
):
    """Upload a folder to a NodeODM node, process, download & extract results.
    Returns a dict of local output paths (orthophoto/dsm/dtm/point_cloud/mesh)."""
    client = NodeODMClient(base_url, token=token)

    feedback("[STEP 1/5] Connecting to the NodeODM node...")
    client.info()

    images = find_images(input_dir)
    files = list(images)
    if gcp_path and os.path.isfile(gcp_path):
        files.append(gcp_path)  # NodeODM auto-detects a gcp_list.txt among the files
    if len(images) < 2:
        raise NodeODMError("Need at least 2 images in %s." % input_dir)

    options = [{"name": "orthophoto-resolution", "value": float(ortho_resolution_cm)}]
    if produce_dsm:
        options.append({"name": "dsm", "value": True})
    if produce_dtm:
        options.append({"name": "dtm", "value": True})
    if not produce_mesh:
        options.append({"name": "skip-3dmodel", "value": True})
    if fast_orthophoto:
        options.append({"name": "fast-orthophoto", "value": True})
    if pc_quality:
        options.append({"name": "pc-quality", "value": pc_quality})
    if dem_resolution_cm:
        options.append({"name": "dem-resolution", "value": float(dem_resolution_cm)})

    feedback("[STEP 2/5] Creating task...")
    uuid = client.new_task_init(options=options, name=name)

    feedback("[STEP 3/5] Uploading %d file(s)..." % len(files))
    for i, p in enumerate(files):
        if is_canceled():
            client.cancel(uuid)
            raise NodeODMCanceled()
        client.upload_image(uuid, p)
        progress(int((i + 1) / float(len(files)) * 15))
    client.commit(uuid)

    feedback("[STEP 4/5] Processing on the node (this can take a while)...")
    client.wait_for_task(uuid, feedback=feedback,
                        progress=lambda p: progress(15 + int(p * 0.75)),
                        is_canceled=is_canceled)

    feedback("[STEP 5/5] Downloading & extracting results...")
    os.makedirs(output_dir, exist_ok=True)
    zip_path = os.path.join(output_dir, "all.zip")
    client.download_all(uuid, zip_path, feedback=feedback)
    extract_dir = os.path.join(output_dir, "results")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)
    progress(98)

    assets = collect_assets(extract_dir)
    assets["all_zip"] = zip_path
    assets["results_dir"] = extract_dir
    assets["task_uuid"] = uuid
    for k in ("orthophoto", "dsm", "dtm", "point_cloud", "mesh"):
        if assets.get(k):
            feedback("[INFO] %s -> %s" % (k, assets[k]))
    if not assets.get("orthophoto"):
        feedback("[WARNING] No orthophoto found in the results; check the node console output.")
    progress(100)
    return assets


def _main_cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Process a folder of drone photos on a NodeODM node.")
    ap.add_argument("--url", required=True, help="NodeODM base URL, e.g. http://localhost:3000")
    ap.add_argument("--token", default=None)
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--test", action="store_true", help="Only test the connection.")
    ap.add_argument("--no-dsm", action="store_true")
    ap.add_argument("--no-dtm", action="store_true")
    ap.add_argument("--no-mesh", action="store_true")
    ap.add_argument("--pc-quality", default="medium")
    ap.add_argument("--ortho-resolution", type=float, default=5.0)
    ap.add_argument("--gcp", default=None)
    args = ap.parse_args(argv)

    if args.test:
        ok, msg = NodeODMClient(args.url, token=args.token).test_connection()
        print(msg)
        return 0 if ok else 1

    if not args.input or not args.output:
        ap.error("--input and --output are required unless --test")
    out = process_folder(
        args.url, args.input, args.output, token=args.token,
        produce_dsm=not args.no_dsm, produce_dtm=not args.no_dtm, produce_mesh=not args.no_mesh,
        pc_quality=args.pc_quality, ortho_resolution_cm=args.ortho_resolution, gcp_path=args.gcp,
        feedback=lambda m: print(m), progress=lambda p: None,
    )
    print("\nOutputs:")
    for k, v in out.items():
        print("  %s: %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(_main_cli())
