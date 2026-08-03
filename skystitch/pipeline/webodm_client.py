"""
webodm_client.py
================
A small client for the **WebODM REST API**, used by SkyStitch's "WebODM
(remote)" engine so drone photos can be processed on a WebODM server -- local
(the free native Windows installer) or remote -- **without Docker on the
user's machine**.

Flow (mirrors what the WebODM web UI does, using the resumable/partial upload
so hundreds of photos don't have to be held in memory at once):

    authenticate  -> JWT token
    create_project
    create_task(partial=true)          -> task id
    upload each image one request each -> low memory, real upload progress
    commit_task                        -> processing starts
    wait (poll status + running_progress, stream console output)
    download the requested assets (orthophoto.tif, dsm.tif, dtm.tif,
        georeferenced_model.laz, textured_model.zip, ...)

Only depends on ``requests`` (commonly present in QGIS's Python; if missing,
the dialog shows a one-line pip install hint). No QGIS imports here, so it can
also be exercised as a CLI.
"""

import json
import logging
import os
import sys
import time

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


# WebODM task status codes (from the WebODM API).
STATUS = {10: "QUEUED", 20: "RUNNING", 30: "FAILED", 40: "COMPLETED", 50: "CANCELED"}

# Human asset name -> WebODM download filename.
ASSETS = {
    "orthophoto": "orthophoto.tif",
    "dsm": "dsm.tif",
    "dtm": "dtm.tif",
    "point_cloud": "georeferenced_model.laz",
    "mesh": "textured_model.zip",
    "report": "report.pdf",
    "all": "all.zip",
}

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def _noop(*_a, **_k):
    pass


def _true(*_a, **_k):
    return False


class WebODMError(Exception):
    pass


class WebODMCanceled(Exception):
    pass


def requests_available():
    return requests is not None


class WebODMClient:

    def __init__(self, base_url, username="admin", password=None, token=None, timeout=60):
        if requests is None:
            raise WebODMError(
                "The Python package 'requests' is not installed in QGIS's Python. "
                "Install it once via the OSGeo4W Shell:\n"
                "    python-qgis -m pip install requests"
            )
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password or ""
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()

    # -------------------------------------------------------------- helpers
    def _url(self, path):
        return "%s/%s" % (self.base, path.lstrip("/"))

    def _headers(self):
        return {"Authorization": "JWT %s" % self.token} if self.token else {}

    def _check(self, r, what):
        if r.status_code >= 400:
            body = ""
            try:
                body = json.dumps(r.json())
            except Exception:
                body = (r.text or "")[:500]
            raise WebODMError("%s failed (HTTP %s): %s" % (what, r.status_code, body))
        return r

    # -------------------------------------------------------------- API
    def authenticate(self):
        if self.token:
            return self.token
        try:
            r = self.session.post(
                self._url("/api/token-auth/"),
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except Exception as e:
            raise WebODMError(
                "Could not reach the WebODM server at %s (%s). Is WebODM running "
                "and the URL correct?" % (self.base, e)
            )
        self._check(r, "Login")
        self.token = r.json().get("token")
        if not self.token:
            raise WebODMError("Login returned no token; check the username/password.")
        return self.token

    def test_connection(self):
        """Return (ok, message)."""
        try:
            self.authenticate()
            r = self.session.get(self._url("/api/projects/"), headers=self._headers(), timeout=self.timeout)
            self._check(r, "List projects")
            return True, "OK - connected to WebODM at %s and logged in as '%s'." % (self.base, self.username)
        except WebODMError as e:
            return False, str(e)
        except Exception as e:
            return False, "Connection failed: %s" % e

    def create_project(self, name="SkyStitch"):
        r = self.session.post(self._url("/api/projects/"), headers=self._headers(),
                              data={"name": name}, timeout=self.timeout)
        self._check(r, "Create project")
        return r.json()["id"]

    def create_task(self, project_id, options=None, name="SkyStitch task"):
        """Create a *partial* task (no images yet) so images can be streamed
        one-by-one, then committed."""
        data = {"partial": "true", "name": name}
        if options:
            data["options"] = json.dumps(options)
        r = self.session.post(self._url("/api/projects/%s/tasks/" % project_id),
                              headers=self._headers(), data=data, timeout=self.timeout)
        self._check(r, "Create task")
        return r.json()["id"]

    def upload_image(self, project_id, task_id, path):
        with open(path, "rb") as fh:
            files = {"images": (os.path.basename(path), fh)}
            r = self.session.post(
                self._url("/api/projects/%s/tasks/%s/upload/" % (project_id, task_id)),
                headers=self._headers(), files=files, timeout=max(self.timeout, 300),
            )
        self._check(r, "Upload %s" % os.path.basename(path))

    def commit_task(self, project_id, task_id):
        r = self.session.post(
            self._url("/api/projects/%s/tasks/%s/commit/" % (project_id, task_id)),
            headers=self._headers(), timeout=self.timeout)
        self._check(r, "Commit task")

    def task_info(self, project_id, task_id):
        r = self.session.get(self._url("/api/projects/%s/tasks/%s/" % (project_id, task_id)),
                            headers=self._headers(), timeout=self.timeout)
        self._check(r, "Task info")
        return r.json()

    def task_output(self, project_id, task_id, line=0):
        try:
            r = self.session.get(
                self._url("/api/projects/%s/tasks/%s/output/" % (project_id, task_id)),
                headers=self._headers(), params={"line": line}, timeout=self.timeout)
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

    def cancel_task(self, project_id, task_id):
        try:
            self.session.post(self._url("/api/projects/%s/tasks/%s/cancel/" % (project_id, task_id)),
                             headers=self._headers(), timeout=self.timeout)
        except Exception:
            logging.getLogger(__name__).debug("cancel request failed (best-effort)", exc_info=True)

    @staticmethod
    def _status_code(info):
        st = info.get("status")
        if isinstance(st, dict):
            return st.get("code")
        return st

    def wait_for_task(self, project_id, task_id, feedback=_noop, progress=_noop,
                      is_canceled=_true, poll_secs=5):
        """Poll until the task finishes; stream new console lines; return the
        final task info. Raises on failure/cancel."""
        seen = 0
        last_pct = -1
        while True:
            if is_canceled():
                feedback("[INFO] Cancel requested -- stopping the WebODM task...")
                self.cancel_task(project_id, task_id)
                raise WebODMCanceled()

            info = self.task_info(project_id, task_id)
            for ln in self.task_output(project_id, task_id, line=seen):
                feedback(ln)
                seen += 1

            code = self._status_code(info)
            rp = info.get("running_progress") or 0
            pct = int(round(float(rp) * 100))
            if pct != last_pct:
                progress(min(99, pct))
                last_pct = pct

            if code == 40:
                progress(100)
                feedback("[INFO] WebODM task completed.")
                return info
            if code == 30:
                raise WebODMError("WebODM task failed: %s" % (info.get("last_error") or "see server logs"))
            if code == 50:
                raise WebODMCanceled()

            time.sleep(poll_secs)

    def available_assets(self, project_id, task_id):
        info = self.task_info(project_id, task_id)
        return info.get("available_assets", []) or []

    def download_asset(self, project_id, task_id, asset_filename, dest_path,
                       feedback=_noop):
        url = self._url("/api/projects/%s/tasks/%s/download/%s" % (project_id, task_id, asset_filename))
        r = self.session.get(url, headers=self._headers(), stream=True, timeout=max(self.timeout, 600))
        if r.status_code >= 400:
            feedback("[INFO] Asset not available: %s (HTTP %s)" % (asset_filename, r.status_code))
            return None
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
        feedback("[INFO] Downloaded %s -> %s" % (asset_filename, dest_path))
        return dest_path


def find_images(folder, exts=IMAGE_EXTS):
    out = []
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith(exts):
            out.append(os.path.join(folder, fn))
    return out


def process_folder(
    base_url, username, password, input_dir, output_dir,
    produce_dsm=True, produce_dtm=True, produce_mesh=True,
    pc_quality="medium", ortho_resolution_cm=5.0, dem_resolution_cm=None,
    fast_orthophoto=False, gcp_path=None, project_name="SkyStitch",
    download=("orthophoto", "dsm", "dtm", "point_cloud"),
    feedback=_noop, progress=_noop, is_canceled=_true,
):
    """End-to-end: upload a folder to WebODM, process, download assets.
    Returns a dict of downloaded output paths (keys: orthophoto/dsm/dtm/...)."""
    client = WebODMClient(base_url, username=username, password=password)

    feedback("[STEP 1/5] Logging in to WebODM...")
    client.authenticate()

    images = find_images(input_dir)
    if gcp_path and os.path.isfile(gcp_path):
        images = images + [gcp_path]  # WebODM auto-detects a gcp_list.txt among the files
    if len([p for p in images if p.lower().endswith(IMAGE_EXTS)]) < 2:
        raise WebODMError("Need at least 2 images in %s." % input_dir)

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

    feedback("[STEP 2/5] Creating project & task...")
    pid = client.create_project(project_name)
    tid = client.create_task(pid, options=options, name=project_name)

    feedback("[STEP 3/5] Uploading %d file(s)..." % len(images))
    for i, p in enumerate(images):
        if is_canceled():
            client.cancel_task(pid, tid)
            raise WebODMCanceled()
        client.upload_image(pid, tid, p)
        progress(int((i + 1) / float(len(images)) * 15))  # uploads = first ~15% of the bar
    client.commit_task(pid, tid)

    feedback("[STEP 4/5] Processing on the server (this can take a while)...")
    client.wait_for_task(pid, tid, feedback=feedback,
                         progress=lambda p: progress(15 + int(p * 0.8)),
                         is_canceled=is_canceled)

    feedback("[STEP 5/5] Downloading results...")
    avail = set(client.available_assets(pid, tid))
    out = {"project_id": pid, "task_id": tid}
    for key in download:
        fname = ASSETS.get(key)
        if not fname:
            continue
        if avail and fname not in avail:
            feedback("[INFO] %s not produced by this task; skipping." % fname)
            continue
        dest = os.path.join(output_dir, fname)
        got = client.download_asset(pid, tid, fname, dest, feedback=feedback)
        if got:
            out[key] = got
    progress(100)
    return out


def _main_cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Process a folder of drone photos on a WebODM server.")
    ap.add_argument("--url", required=True, help="WebODM base URL, e.g. http://localhost:8000")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--test", action="store_true", help="Only test the connection, then exit.")
    ap.add_argument("--no-dsm", action="store_true")
    ap.add_argument("--no-dtm", action="store_true")
    ap.add_argument("--no-mesh", action="store_true")
    ap.add_argument("--pc-quality", default="medium")
    ap.add_argument("--ortho-resolution", type=float, default=5.0)
    ap.add_argument("--gcp", default=None)
    args = ap.parse_args(argv)

    if args.test:
        ok, msg = WebODMClient(args.url, args.user, args.password).test_connection()
        print(msg)
        return 0 if ok else 1

    out = process_folder(
        args.url, args.user, args.password, args.input, args.output,
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
