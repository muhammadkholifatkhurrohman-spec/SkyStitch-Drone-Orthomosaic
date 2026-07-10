"""
core.py
=======
"Library" version of the original build_orthomosaic.py CLI script. The
pipeline logic is EXACTLY THE SAME, just wrapped into a single
`run_pipeline(...)` function that can be called from anywhere (including
from a QGIS plugin / QgsTask), with:
  - `feedback(msg: str)`  -> callback to send progress text
  - `is_canceled()`       -> callback to check whether the user canceled
  - `progress(pct: float)`-> callback to update the progress bar (0-100)

It can also still be run directly as a CLI (python3 core.py ...).
"""

import glob
import os
import re
import sys

from . import proj_fix  # noqa: F401 - must run before rasterio touches PROJ

import cv2
import numpy as np
import rasterio
from rasterio.transform import Affine

from .geo_utils import GeoConverter, read_gps, estimate_analytic_gsd
from .feature_matching import build_match_graph
from .mosaic_builder import chain_transforms, fit_world_similarity, render_mosaic


class PipelineCanceled(Exception):
    """Raised when the user cancels the process midway."""


class PipelineError(Exception):
    """Error that has already been translated into a user-friendly message for the GUI."""


def _noop_feedback(msg):
    pass


def _noop_progress(pct):
    pass


def _noop_is_canceled():
    return False


def find_photos(input_dir, pattern):
    """Find photos matching `pattern` inside `input_dir`.

    Supports multiple patterns separated by ';' or ',' (e.g. "*.jpg;*.jpeg"),
    and also tries the upper-case version of each pattern so a folder with
    mixed-case extensions (common when photos come from different apps/OSes)
    isn't silently missing files on case-sensitive filesystems. Matches are
    de-duplicated and returned in a stable sorted order.
    """
    sub_patterns = [p.strip() for p in re.split(r"[;,]", pattern) if p.strip()]
    if not sub_patterns:
        sub_patterns = ["*.jpg"]

    seen = set()
    paths = []
    for pat in sub_patterns:
        for candidate in (pat, pat.upper(), pat.lower()):
            for p in glob.glob(os.path.join(input_dir, candidate)):
                key = os.path.normcase(os.path.abspath(p))
                if key not in seen:
                    seen.add(key)
                    paths.append(p)
    return sorted(paths)


def run_pipeline(
    input_dir,
    output_path,
    pattern="*.jpg",
    max_photos=None,
    gsd=None,
    feedback=_noop_feedback,
    progress=_noop_progress,
    is_canceled=_noop_is_canceled,
):
    """Run the whole orthomosaic pipeline. Returns the resulting .tif path.
    Raises PipelineError on failure, or PipelineCanceled if canceled."""

    def check_cancel():
        if is_canceled():
            raise PipelineCanceled()

    photo_paths = find_photos(input_dir, pattern)
    if max_photos:
        photo_paths = photo_paths[:max_photos]

    if len(photo_paths) < 2:
        raise PipelineError("At least 2 overlapping photos are required to build a mosaic.")

    feedback(f"Found {len(photo_paths)} photos.")
    check_cancel()

    # ---------- Step 1: read GPS ----------
    feedback("[STEP 1/6] Reading GPS position from EXIF...")
    progress(5)
    gps_list = []
    valid_paths = []
    for p in photo_paths:
        gps = read_gps(p)
        if gps is None:
            feedback(f"  [WARNING] '{os.path.basename(p)}' has no GPS data, skipping.")
            continue
        gps_list.append(gps)
        valid_paths.append(p)
    check_cancel()

    if len(valid_paths) < 2:
        raise PipelineError("Fewer than 2 photos have valid GPS EXIF data.")

    photo_paths = valid_paths
    geo = GeoConverter(gps_list[0]["lon"], gps_list[0]["lat"])
    world_xy = [geo.latlon_to_xy(g["lon"], g["lat"]) for g in gps_list]
    feedback(f"  Using reference projection: {geo.utm_crs}")
    progress(10)

    # ---------- Step 2: load images & detect/match features ----------
    feedback("[STEP 2/6] Loading photos & detecting features (SIFT)...")
    images_color = []
    images_gray = []
    loaded_paths = []
    loaded_world_xy = []
    loaded_gps = []
    for p, xy, gps in zip(photo_paths, world_xy, gps_list):
        check_cancel()
        img = cv2.imread(p)
        if img is None:
            feedback(f"  [WARNING] Failed to open '{os.path.basename(p)}', skipping.")
            continue
        images_color.append(img)
        images_gray.append(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        loaded_paths.append(p)
        loaded_world_xy.append(xy)
        loaded_gps.append(gps)
    # BUGFIX: photo_paths/gps_list/world_xy must stay index-aligned with
    # images_color/images_gray from this point on, otherwise a single
    # unreadable photo would silently shift every later index and pair
    # each remaining photo with the wrong GPS position.
    photo_paths = loaded_paths
    world_xy = loaded_world_xy
    gps_list = loaded_gps

    if len(photo_paths) < 2:
        raise PipelineError("Fewer than 2 photos could be opened successfully.")
    progress(20)

    check_cancel()
    features, edges = build_match_graph(
        images_gray, feedback=lambda m: feedback("  " + m), check_cancel=check_cancel
    )
    check_cancel()

    if not edges:
        raise PipelineError(
            "No photos could be matched to each other. "
            "Make sure the photos actually overlap (the same area appears in more than 1 photo)."
        )
    progress(55)

    # ---------- Step 3: chain transforms between photos ----------
    feedback("[STEP 3/6] Chaining relative transforms between photos...")
    n = len(images_color)
    root, rel_transforms, connected = chain_transforms(n, edges)
    n_connected = sum(connected)
    feedback(f"  {n_connected}/{n} photos successfully chained into one group (root = photo {root}).")
    if n_connected < n:
        skipped = [os.path.basename(photo_paths[i]) for i in range(n) if not connected[i]]
        feedback(f"  [WARNING] The following photos don't overlap with the main group and were skipped: {skipped}")
    progress(65)
    check_cancel()

    # ---------- Step 4: correct global position using GPS ----------
    feedback("[STEP 4/6] Adjusting position using real GPS coordinates...")
    analytic_gsds = [
        estimate_analytic_gsd(photo_paths[i], gps_list[i]["alt"])
        for i in range(len(photo_paths))
        if connected[i]
    ]
    analytic_gsds = [g for g in analytic_gsds if g is not None]
    analytic_gsd = float(np.median(analytic_gsds)) if analytic_gsds else None
    if analytic_gsd:
        feedback(f"  Analytic GSD estimate from camera parameters: {analytic_gsd:.4f} m/px")

    world_similarity = fit_world_similarity(
        images_color, rel_transforms, connected, world_xy,
        analytic_gsd=analytic_gsd, feedback=lambda m: feedback("  " + m),
    )
    feedback("  Pixel -> world coordinate transform computed successfully.")
    progress(75)
    check_cancel()

    # ---------- Step 5: render mosaic (warp + blend) ----------
    feedback("[STEP 5/6] Merging all photos (warp & blend)...")
    mosaic, transform_world, gsd_used = render_mosaic(
        images_color, rel_transforms, connected, world_similarity, gsd=gsd
    )
    feedback(f"  Mosaic size: {mosaic.shape[1]} x {mosaic.shape[0]} px, GSD = {gsd_used:.4f} m/px")
    progress(90)
    check_cancel()

    # ---------- Step 6: save as GeoTIFF ----------
    feedback("[STEP 6/6] Saving result as GeoTIFF...")
    _save_geotiff(output_path, mosaic, transform_world, geo.utm_crs)
    preview_path = _save_preview(output_path)
    feedback(f"[DONE] Orthomosaic saved to: {output_path}")
    feedback(f"[DONE] Preview saved to: {preview_path}")
    progress(100)

    return output_path


def _save_geotiff(path, mosaic_bgr, world_transform_2x3, crs):
    h, w = mosaic_bgr.shape[:2]
    rgb = cv2.cvtColor(mosaic_bgr, cv2.COLOR_BGR2RGB)

    alpha = (rgb.sum(axis=2) > 0).astype(np.uint8) * 255

    a, b, c = world_transform_2x3[0]
    d, e, f = world_transform_2x3[1]
    transform = Affine(a, b, c, d, e, f)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with proj_fix.rasterio_env():
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=h,
            width=w,
            count=4,
            dtype=rasterio.uint8,
            crs=crs,
            transform=transform,
            compress="deflate",
            tiled=True,
            BIGTIFF="IF_SAFER",
        ) as dst:
            for i in range(3):
                dst.write(rgb[:, :, i], i + 1)
            dst.write(alpha, 4)


def _save_preview(path, preview_width=1600):
    from PIL import Image

    with proj_fix.rasterio_env(), rasterio.open(path) as src:
        scale = preview_width / src.width
        preview_height = max(1, int(src.height * scale))
        data = src.read(out_shape=(min(src.count, 4), preview_height, preview_width))
        n_bands = min(3, data.shape[0])
        img_arr = np.transpose(data[:n_bands], (1, 2, 0))

    preview_path = os.path.splitext(path)[0] + "_preview.jpg"
    Image.fromarray(img_arr).save(preview_path, quality=90)
    return preview_path


# ---- can still be used as a CLI, same as the original build_orthomosaic.py ----
def _main_cli():
    import argparse

    parser = argparse.ArgumentParser(description="Build an orthomosaic from raw drone photos (JPG + GPS EXIF).")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pattern", default="*.jpg")
    parser.add_argument("--max-photos", type=int, default=None)
    parser.add_argument("--gsd", type=float, default=None)
    args = parser.parse_args()

    try:
        run_pipeline(
            args.input, args.output, args.pattern, args.max_photos, args.gsd,
            feedback=print, progress=lambda p: None, is_canceled=lambda: False,
        )
    except (PipelineError, PipelineCanceled) as e:
        print(f"[FAILED] {e}")
        sys.exit(1)


if __name__ == "__main__":
    _main_cli()
