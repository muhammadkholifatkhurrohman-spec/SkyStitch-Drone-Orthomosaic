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
from rasterio.enums import ColorInterp

from .geo_utils import (
    GeoConverter, read_gps, read_xmp_gimbal_yaw, estimate_analytic_gsd,
    assess_terrain_relief, DEFAULT_SENSOR_WIDTH_MM,
)
from .feature_matching import build_match_graph
from .mosaic_builder import (
    chain_transforms, fit_world_similarity, render_mosaic, compute_exposure_gains,
    apply_exposure_gains, UnreliableGeoreferenceError,
)
from .gcp_icp import load_gcp_points, apply_gcp_corrections, GcpError


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
    exposure_compensation=True,
    gcp_path=None,
    compression="deflate",
    jpeg_quality=85,
    feedback=_noop_feedback,
    progress=_noop_progress,
    is_canceled=_noop_is_canceled,
):
    """Run the whole orthomosaic pipeline. Returns the resulting .tif path.
    Raises PipelineError on failure, or PipelineCanceled if canceled.

    If only one photo has valid GPS EXIF data, feature-matching/stitching
    is skipped entirely and that single photo is georeferenced directly
    from its own GPS position (see _georeference_single_photo) -- this
    still requires an analytic GSD estimate from the photo's EXIF (focal
    length, image width, altitude); if that can't be computed, the run
    fails rather than guessing a scale.

    gcp_path: optional path to a CSV/XLSX file with surveyed Ground
    Control Points for specific photos, used as trusted anchors in an
    iterative (GCP/ICP) refinement of the GPS-based position/scale/
    rotation fit -- see pipeline/gcp_icp.py. Leave as None to use GPS
    only (previous behavior, unaffected).

    compression: GeoTIFF compression method for the output raster --
    one of "deflate" (lossless, previous default behavior), "lzw"
    (lossless), "zstd" (lossless, usually smaller & faster than
    deflate/lzw if GDAL was built with ZSTD support), "jpeg" (lossy,
    small files -- see _save_geotiff for the alpha-band caveat), "jp2"
    (JPEG2000, lossy, usually the smallest files of all with a real
    alpha band -- written via GDAL's JP2OpenJPEG driver and saved with a
    .jp2 extension regardless of `output_path`'s extension; falls back
    to "deflate" if this GDAL build lacks OpenJPEG support), or "none"
    (no compression, largest files).

    jpeg_quality: 1-100, only used when compression="jpeg". Higher =
    better quality & bigger file."""

    def check_cancel():
        if is_canceled():
            raise PipelineCanceled()

    photo_paths = find_photos(input_dir, pattern)
    if max_photos:
        photo_paths = photo_paths[:max_photos]

    if len(photo_paths) < 1:
        raise PipelineError("No photos matched in the input folder.")

    feedback(f"Found {len(photo_paths)} photos.")
    check_cancel()

    # ---------- Step 1: read GPS ----------
    feedback("[STEP 1/7] Reading GPS position from EXIF...")
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

    if len(valid_paths) < 1:
        raise PipelineError("None of the photos have valid GPS EXIF data.")

    if len(valid_paths) == 1:
        # Only one usable photo -- there's nothing to stitch/feature-match
        # against, but it still has a real GPS position, so it's worth
        # georeferencing directly rather than refusing outright. This
        # skips feature matching, chaining, GCP/ICP and exposure
        # compensation entirely (all of those need >=2 photos) and instead
        # places the single photo using its own GPS position, an
        # analytic (camera-parameter-based) GSD, and its gimbal yaw if
        # available. See _georeference_single_photo.
        return _georeference_single_photo(
            valid_paths[0], gps_list[0], output_path,
            compression=compression, jpeg_quality=jpeg_quality,
            feedback=feedback, progress=progress,
        )

    relief_warning = assess_terrain_relief(gps_list)
    if relief_warning:
        feedback(f"  [WARNING] {relief_warning}")

    photo_paths = valid_paths
    geo = GeoConverter(gps_list[0]["lon"], gps_list[0]["lat"])
    world_xy = [geo.latlon_to_xy(g["lon"], g["lat"]) for g in gps_list]
    feedback(f"  Using reference projection: {geo.utm_crs}")
    progress(10)

    # ---------- Optional: apply GCP/ICP corrections ----------
    # gcp_weights/gcp_mask are always built (defaulting to "no GCP"), so
    # the rest of the pipeline can treat the GCP and GPS-only cases
    # uniformly instead of branching on whether gcp_path was given.
    gcp_weights = [1.0] * len(photo_paths)
    gcp_mask = [False] * len(photo_paths)
    if gcp_path:
        feedback(f"  Loading GCP/ICP correction file: {os.path.basename(gcp_path)}")
        try:
            gcp_points = load_gcp_points(gcp_path)
        except GcpError as e:
            raise PipelineError(f"GCP file error: {e}")
        world_xy, gcp_weights, gcp_mask, n_matched, _unmatched = apply_gcp_corrections(
            photo_paths, world_xy, geo, gcp_points, feedback=lambda m: feedback("  " + m)
        )
        n_anchored_photos = sum(gcp_mask)
        feedback(f"  Matched {n_matched}/{len(gcp_points)} GCP point(s) to {n_anchored_photos} input photo(s).")
        if n_matched == 0:
            feedback(
                "  [WARNING] No GCP points matched any input photo by filename -- "
                "GCP/ICP correction will have no effect. Check the 'photo' column "
                "matches the actual photo filenames."
            )
    check_cancel()

    # ---------- Step 2: load images & detect/match features ----------
    feedback("[STEP 2/7] Loading photos & detecting features (SIFT)...")
    images_color = []
    images_gray = []
    loaded_paths = []
    loaded_world_xy = []
    loaded_gps = []
    loaded_gcp_weights = []
    loaded_gcp_mask = []
    for p, xy, gps, gw, gm in zip(photo_paths, world_xy, gps_list, gcp_weights, gcp_mask):
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
        loaded_gcp_weights.append(gw)
        loaded_gcp_mask.append(gm)
    # BUGFIX: photo_paths/gps_list/world_xy (and gcp_weights/gcp_mask) must
    # stay index-aligned with images_color/images_gray from this point on,
    # otherwise a single unreadable photo would silently shift every later
    # index and pair each remaining photo with the wrong GPS/GCP data.
    photo_paths = loaded_paths
    world_xy = loaded_world_xy
    gps_list = loaded_gps
    gcp_weights = loaded_gcp_weights
    gcp_mask = loaded_gcp_mask

    if len(photo_paths) < 2:
        raise PipelineError("Fewer than 2 photos could be opened successfully.")
    progress(20)

    check_cancel()
    features, edges = build_match_graph(
        images_gray, positions=world_xy, feedback=lambda m: feedback("  " + m), check_cancel=check_cancel
    )
    check_cancel()

    if not edges:
        feedback(
            "  [NOTICE] No photos could be matched to each other (no overlap detected between "
            "any pair). Every photo will be placed on the canvas using its GPS position only, "
            "with no feature-matching/blending refinement."
        )
    progress(55)

    # ---------- Step 3: chain transforms between photos ----------
    feedback("[STEP 3/7] Chaining relative transforms between photos...")
    n = len(images_color)
    root, rel_transforms, connected = chain_transforms(n, edges)
    n_connected = sum(connected)
    feedback(f"  {n_connected}/{n} photos successfully chained into one group (root = photo {root}).")
    if n_connected < n:
        gps_only_names = [os.path.basename(photo_paths[i]) for i in range(n) if not connected[i]]
        feedback(
            f"  [NOTICE] The following photos don't overlap with the main matched group. They are "
            f"NOT excluded from the result -- they will still be placed on the canvas at their own "
            f"GPS coordinates (no feature-matching/blending refinement for them): {gps_only_names}"
        )
    progress(65)
    check_cancel()

    # ---------- Step 4: correct global position using GPS ----------
    feedback("[STEP 4/7] Adjusting position using real GPS coordinates...")
    camera_estimates = [
        estimate_analytic_gsd(photo_paths[i], gps_list[i]["alt"])
        for i in range(len(photo_paths))
        if connected[i]
    ]
    analytic_gsds = [c["gsd"] for c in camera_estimates if c["gsd"] is not None]
    analytic_gsd = float(np.median(analytic_gsds)) if analytic_gsds else None

    # An unrecognized camera model means SENSOR_WIDTH_MM had to guess a
    # default sensor width, which can make the analytic GSD estimate
    # meaningfully wrong -- worth telling the user, especially since this
    # estimate is silently trusted as the scale fallback whenever the GPS
    # baseline between photos is too tight to calibrate scale on its own
    # (see fit_world_similarity / MIN_RELIABLE_BASELINE_M).
    unknown_cameras = sorted({
        (c["make"], c["model"]) for c in camera_estimates if not c["sensor_known"]
    })
    if unknown_cameras and analytic_gsds:
        names = ", ".join(f"{make or '?'} {model or '?'}".strip() for make, model in unknown_cameras)
        feedback(
            f"  [WARNING] Unrecognized camera model(s) ({names}) -- sensor width was guessed as "
            f"{DEFAULT_SENSOR_WIDTH_MM} mm. The analytic GSD estimate below may be inaccurate for "
            f"these photos; if the mosaic's scale looks off, consider setting the GSD manually."
        )

    if analytic_gsd:
        feedback(f"  Analytic GSD estimate from camera parameters: {analytic_gsd:.4f} m/px")

    # Only needed as an orientation fallback when the GPS baseline is too
    # tight to fit rotation from the data (see fit_world_similarity); a
    # single extra file read for the root photo only, so it's cheap to
    # always try. None if the drone doesn't expose gimbal yaw in XMP.
    root_yaw_deg = read_xmp_gimbal_yaw(photo_paths[root])

    try:
        world_similarity = fit_world_similarity(
            images_color, rel_transforms, connected, world_xy,
            analytic_gsd=analytic_gsd, root_yaw_deg=root_yaw_deg,
            point_weights=gcp_weights, gcp_mask=gcp_mask,
            feedback=lambda m: feedback("  " + m),
        )
    except UnreliableGeoreferenceError as e:
        # Every other georeferenced case (multiple GPS points, non-overlapping
        # photos placed by GPS, GCP/ICP refinement, single-photo runs, etc.)
        # still runs and gets saved -- this is the one situation where there
        # genuinely isn't enough information to trust the result, so it's
        # reported as a failure instead of writing a GeoTIFF that wouldn't
        # match the real-world layout.
        raise PipelineError(str(e))
    feedback("  Pixel -> world coordinate transform computed successfully.")

    # ---------- Step 4b: place non-overlapping photos using GPS only ----------
    # `connected` above only marks photos that were successfully feature-matched
    # into the main chained group -- but a photo not overlapping with that group
    # is still a real photo with a real GPS position, so it's placed directly at
    # its own GPS coordinate (same scale/rotation as the fitted mosaic, since we
    # have no matching info to refine its own orientation) rather than being
    # dropped from the output. `matched` keeps track of which is which, purely
    # for logging/notification -- `connected` is flipped to True for these
    # photos afterwards so steps 5-6 (exposure compensation, warp & blend)
    # render them exactly like any feature-matched photo.
    matched = list(connected)
    if not all(matched):
        R_s = world_similarity[:2, :2]
        t_s = world_similarity[:2, 2]
        try:
            R_s_inv = np.linalg.inv(R_s)
        except np.linalg.LinAlgError:
            R_s_inv = np.eye(2)
        for i in range(n):
            if matched[i]:
                continue
            h, w = images_color[i].shape[:2]
            center = np.array([w / 2.0, h / 2.0])
            d = R_s_inv @ (np.array(world_xy[i]) - t_s - R_s @ center)
            rel_transforms[i] = np.array(
                [[1.0, 0.0, d[0]], [0.0, 1.0, d[1]], [0.0, 0.0, 1.0]]
            )
            connected[i] = True
        feedback(
            f"  [NOTICE] Placed {n - sum(matched)} non-overlapping photo(s) on the canvas at their "
            f"GPS coordinates. Overlap between these and other photos (if any) is not a problem -- "
            f"they'll simply be blended/seamed like any other overlapping pair."
        )
    progress(75)
    check_cancel()

    # ---------- Step 5: exposure compensation ----------
    if exposure_compensation:
        feedback("[STEP 5/7] Compensating brightness differences between photos...")
        gains = compute_exposure_gains(images_color, connected, feedback=lambda m: feedback("  " + m))
        images_color = apply_exposure_gains(images_color, gains)
    progress(80)
    check_cancel()

    # ---------- Step 6: render mosaic (warp + blend) ----------
    feedback("[STEP 6/7] Merging all photos (warp & blend)...")
    mosaic, transform_world, gsd_used, coverage_mask = render_mosaic(
        images_color, rel_transforms, connected, world_similarity, gsd=gsd, feedback=lambda m: feedback("  " + m)
    )
    feedback(f"  Mosaic size: {mosaic.shape[1]} x {mosaic.shape[0]} px, GSD = {gsd_used:.4f} m/px")
    progress(90)
    check_cancel()

    # ---------- Step 7: save as GeoTIFF ----------
    feedback("[STEP 7/7] Saving result as GeoTIFF...")
    output_path = _save_geotiff(
        output_path, mosaic, transform_world, geo.utm_crs, coverage_mask,
        compression=compression, jpeg_quality=jpeg_quality, feedback=feedback,
    )
    preview_path = _save_preview(output_path)
    feedback(f"[DONE] Orthomosaic saved to: {output_path}")
    feedback(f"[DONE] Preview saved to: {preview_path}")
    feedback(f"[DONE] Final resolution: {gsd_used:.4f} m/px, CRS: {geo.utm_crs}")
    progress(100)

    return output_path, preview_path, gsd_used, geo.utm_crs


def _georeference_single_photo(
    path, gps, output_path,
    compression="deflate", jpeg_quality=85,
    feedback=_noop_feedback, progress=_noop_progress,
):
    """Georeference a single drone photo directly from its own GPS
    position, with no feature-matching/stitching (there's nothing to
    match it against). Scale comes from the analytic GSD estimate (camera
    focal length + sensor width + flight altitude, see
    geo_utils.estimate_analytic_gsd) and orientation comes from the
    photo's gimbal yaw if its EXIF/XMP exposes one.

    If the analytic GSD can't be computed (missing focal length, image
    width, or a valid/positive altitude in the EXIF), there is no reliable
    way to determine real-world scale from one photo alone -- rather than
    guessing, this raises PipelineError so nothing gets saved."""
    feedback(
        "[NOTICE] Only one photo with valid GPS data was found -- skipping feature "
        "matching/stitching and georeferencing it directly from its own GPS position."
    )
    progress(10)

    img = cv2.imread(path)
    if img is None:
        raise PipelineError(f"Failed to open '{os.path.basename(path)}'.")

    geo = GeoConverter(gps["lon"], gps["lat"])
    world_xy = np.array(geo.latlon_to_xy(gps["lon"], gps["lat"]))
    feedback(f"  Using reference projection: {geo.utm_crs}")
    progress(20)

    camera_estimate = estimate_analytic_gsd(path, gps["alt"])
    analytic_gsd = camera_estimate["gsd"]
    if analytic_gsd is None:
        raise PipelineError(
            "Could not georeference this single photo reliably: its EXIF is missing the "
            "focal length, image width, or a valid flight altitude needed to estimate a "
            "real-world scale. With only one photo there's no other way (e.g. a second "
            "overlapping photo) to calibrate scale, so this has been stopped rather than "
            "producing a GeoTIFF with a guessed/incorrect scale."
        )
    if not camera_estimate["sensor_known"]:
        feedback(
            f"  [WARNING] Unrecognized camera model -- sensor width was guessed as "
            f"{DEFAULT_SENSOR_WIDTH_MM} mm. The GSD estimate below may be inaccurate; if the "
            f"output's scale looks off in QGIS, that's the likely reason."
        )
    feedback(f"  Analytic GSD estimate from camera parameters: {analytic_gsd:.4f} m/px")
    progress(35)

    yaw_deg = read_xmp_gimbal_yaw(path)
    h, w = img.shape[:2]
    center = np.array([w / 2.0, h / 2.0])
    s = analytic_gsd
    if yaw_deg is not None:
        feedback(f"  Using this photo's gimbal yaw ({yaw_deg:.1f} deg) for orientation.")
        yaw = np.radians(yaw_deg)
        cos_y, sin_y = np.cos(yaw), np.sin(yaw)
        R = np.array([[cos_y, -sin_y], [-sin_y, -cos_y]])
    else:
        feedback(
            "  [WARNING] No gimbal yaw found in EXIF/XMP -- assuming the photo's top edge "
            "points north. Rotate manually in QGIS afterwards if this looks off."
        )
        R = np.array([[1.0, 0.0], [0.0, -1.0]])

    world_similarity = np.eye(3)
    world_similarity[:2, :2] = s * R
    world_similarity[:2, 2] = world_xy - s * (R @ center)
    progress(50)

    feedback("  Rendering georeferenced photo...")
    mosaic, transform_world, gsd_used, coverage_mask = render_mosaic(
        [img], [np.eye(3)], [True], world_similarity, feedback=lambda m: feedback("  " + m)
    )
    feedback(f"  Output size: {mosaic.shape[1]} x {mosaic.shape[0]} px, GSD = {gsd_used:.4f} m/px")
    progress(85)

    feedback("Saving result as GeoTIFF...")
    output_path = _save_geotiff(
        output_path, mosaic, transform_world, geo.utm_crs, coverage_mask,
        compression=compression, jpeg_quality=jpeg_quality, feedback=feedback,
    )
    preview_path = _save_preview(output_path)
    feedback(f"[DONE] Georeferenced photo saved to: {output_path}")
    feedback(f"[DONE] Preview saved to: {preview_path}")
    feedback(f"[DONE] Final resolution: {gsd_used:.4f} m/px, CRS: {geo.utm_crs}")
    progress(100)

    return output_path, preview_path, gsd_used, geo.utm_crs


# Compression methods offered in the UI -> the exact GDAL creation
# options each one needs. "predictor" only helps deflate/lzw/zstd (it's
# ignored -- and would actually be invalid -- for jpeg/none), so it's
# looked up per-method rather than always set to 2 like the previous
# hardcoded deflate-only version did.
#
# "jp2" (JPEG2000) is NOT in this dict -- it's written through a
# completely different GDAL driver (JP2OpenJPEG) with its own
# creation-option vocabulary (QUALITY/REVERSIBLE, not
# compress/predictor/tiled/BIGTIFF/photometric), so it gets its own
# branch in _save_geotiff below instead of being folded in here.
_COMPRESSION_OPTIONS = {
    "deflate": {"compress": "deflate", "predictor": 2},
    "lzw": {"compress": "lzw", "predictor": 2},
    "zstd": {"compress": "zstd", "predictor": 2},
    "jpeg": {"compress": "jpeg"},
    "none": {"compress": "none"},
}

_JP2_DRIVER = "JP2OpenJPEG"


def _save_geotiff(
    path, mosaic_bgr, world_transform_2x3, crs, coverage_mask,
    compression="deflate", jpeg_quality=85, feedback=_noop_feedback,
):
    """Write the mosaic to disk in the requested format. Returns the
    actual output path used -- normally identical to `path`, except for
    "jp2" (JPEG2000), whose extension is always forced to .jp2 regardless
    of what `path` ends with, since callers (run_pipeline,
    _georeference_single_photo) build `path` from a user-chosen .tif
    filename before the compression method is known to matter for its
    extension. Callers MUST use the returned path for anything after
    this call (preview, "saved to" messages, the return value shown in
    the QGIS UI/loaded as a layer)."""
    h, w = mosaic_bgr.shape[:2]
    rgb = cv2.cvtColor(mosaic_bgr, cv2.COLOR_BGR2RGB)

    # `coverage_mask` comes straight from render_mosaic's per-photo warped
    # footprint masks (see mosaic_builder.py), so it marks exactly which
    # canvas pixels were actually painted by a photo -- unlike a pixel
    # intensity check (e.g. "rgb.sum() > 0"), it doesn't mistake genuinely
    # dark photo content (deep shadows, black rooftops/asphalt, etc.) for
    # empty canvas, so real dark image data won't get punched full of
    # transparent holes.
    alpha = coverage_mask.astype(np.uint8) * 255

    compression = (compression or "deflate").lower()
    if compression not in _COMPRESSION_OPTIONS and compression != "jp2":
        feedback(f"        [WARNING] Unknown compression '{compression}', falling back to 'deflate'.")
        compression = "deflate"

    a, b, c = world_transform_2x3[0]
    d, e, f = world_transform_2x3[1]
    transform = Affine(a, b, c, d, e, f)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    if compression == "jp2":
        try:
            return _write_jp2(path, rgb, alpha, transform, crs, w, h, jpeg_quality, feedback)
        except Exception as e:
            # Most likely cause: this GDAL build wasn't compiled with
            # OpenJPEG support (JP2OpenJPEG is common but not universal
            # across GDAL packagers). Rather than fail the whole run over
            # a compression preference, fall back to the one format
            # that's always present in any GDAL build.
            feedback(
                f"        [WARNING] Could not write JPEG2000 ({e}). This GDAL installation may "
                f"be missing OpenJPEG support. Falling back to DEFLATE compression instead."
            )
            compression = "deflate"
            path = os.path.splitext(path)[0] + ".tif"

    return _write_gtiff(path, rgb, alpha, transform, crs, w, h, compression, jpeg_quality, feedback)


def _write_gtiff(path, rgb, alpha, transform, crs, w, h, compression, jpeg_quality, feedback):
    creation_opts = dict(_COMPRESSION_OPTIONS[compression])
    if compression == "jpeg":
        creation_opts["jpeg_quality"] = int(jpeg_quality)

    # JPEG compression is lossy and (in a single-file GTiff) can't carry a
    # 4th alpha band the way deflate/lzw/zstd/none can -- GDAL would
    # either reject it or silently compress the alpha band as if it were
    # color data, corrupting the transparency mask. So for "jpeg" we
    # write RGB-only (photometric=YCBCR, GDAL's recommended pairing for
    # GTiff+JPEG) and store the coverage mask as a separate internal mask
    # band (.msk) instead -- QGIS and other GDAL-aware viewers still
    # honor that for transparency, it just isn't a literal 4th TIFF band.
    band_count = 3 if compression == "jpeg" else 4
    photometric = "YCBCR" if compression == "jpeg" else "RGB"

    with proj_fix.rasterio_env():
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=h,
            width=w,
            count=band_count,
            dtype=rasterio.uint8,
            crs=crs,
            transform=transform,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            photometric=photometric,
            BIGTIFF="IF_SAFER",
            **creation_opts,
        ) as dst:
            for i in range(3):
                dst.write(rgb[:, :, i], i + 1)
            if compression == "jpeg":
                dst.write_mask(alpha)
            else:
                dst.write(alpha, 4)
                dst.set_band_description(4, "alpha")

            # set_band_description() above only sets a human-readable
            # label -- it does NOT tell GDAL/QGIS that band 4 actually IS
            # the alpha channel. Without this, QGIS's default renderer
            # treats band 4 as just another ordinary band and displays
            # bands 1-3 as-is, so the literal black (0,0,0) pixels that
            # fill the canvas outside each photo's rotated footprint (see
            # render_mosaic in mosaic_builder.py) show up as a solid black
            # border instead of being rendered transparent. Explicitly
            # setting the GDAL color interpretation is what actually makes
            # QGIS (and other GDAL-aware viewers) recognize band 4 as
            # transparency and blend/hide those pixels automatically.
            # (In "jpeg" mode there is no literal band 4 -- the mask
            # written via write_mask() above serves the same purpose.)
            if band_count == 4:
                dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
            else:
                dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue]

            _build_overviews_if_useful(dst, w, h, feedback)

    return path


def _write_jp2(path, rgb, alpha, transform, crs, w, h, quality, feedback):
    """Write the mosaic as JPEG2000 via GDAL's JP2OpenJPEG driver.

    Unlike GTiff+JPEG, JP2OpenJPEG can carry a real 4th alpha band even in
    lossy mode (tested against this build's GDAL), so there's no need for
    the separate .msk-file workaround used for "jpeg" compression above --
    the alpha band and colorinterp are set exactly like the lossless
    GTiff formats.

    Always lossy (REVERSIBLE=NO): lossless JPEG2000 output isn't
    meaningfully smaller than DEFLATE/ZSTD/LZW (which are already offered
    and simpler), so this format's whole reason to exist here is the
    smaller lossy file size. `quality` (1-100, same slider used for the
    "jpeg" option) controls the size/quality trade-off directly.
    """
    path = os.path.splitext(path)[0] + ".jp2"

    with proj_fix.rasterio_env():
        with rasterio.open(
            path,
            "w",
            driver=_JP2_DRIVER,
            height=h,
            width=w,
            count=4,
            dtype=rasterio.uint8,
            crs=crs,
            transform=transform,
            QUALITY=int(quality),
            REVERSIBLE="NO",
        ) as dst:
            for i in range(3):
                dst.write(rgb[:, :, i], i + 1)
            dst.write(alpha, 4)
            dst.set_band_description(4, "alpha")
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]

            _build_overviews_if_useful(dst, w, h, feedback)

    return path


def _build_overviews_if_useful(dst, w, h, feedback):
    # Build internal pyramids/overviews so QGIS (and any other GIS
    # viewer) can render the mosaic smoothly when zoomed out, instead of
    # resampling the full-resolution raster on the fly every time -- this
    # matters a lot for large mosaics (the canvas can easily be
    # 8000x5000px+, see mosaic_builder.py's canvas-size guard). Skipped
    # for small rasters where overviews wouldn't help anyway. Works the
    # same way for both GTiff and JP2OpenJPEG destinations.
    max_side = max(w, h)
    if max_side <= 512:
        return
    factors = []
    factor = 2
    while max_side / factor > 256:
        factors.append(factor)
        factor *= 2
    if factors:
        feedback(f"        Building {len(factors)} overview level(s) for fast display...")
        dst.build_overviews(factors, rasterio.enums.Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")


def _save_preview(path, preview_width=1600):
    """Render a small preview image alongside the .tif output. The
    GeoTIFF itself already carries a proper alpha band (see
    _save_geotiff), so GIS software (QGIS, etc.) already shows the area
    outside the mosaic's rotated footprint as transparent, not black.
    But a plain RGB .jpg preview would drop that alpha band entirely
    (and JPEG can't represent transparency even if it didn't) -- so
    outside a GIS viewer the same area used to show up as a solid black
    border around the tilted mosaic. Saving as .png with the same alpha
    band instead makes that area transparent here too."""
    from PIL import Image

    with proj_fix.rasterio_env(), rasterio.open(path) as src:
        scale = preview_width / src.width
        preview_height = max(1, int(src.height * scale))
        n_bands = min(src.count, 4)
        data = src.read(out_shape=(n_bands, preview_height, preview_width))
        img_arr = np.transpose(data, (1, 2, 0))

    if img_arr.shape[2] < 4:
        # No alpha band available (shouldn't normally happen, since
        # _save_geotiff always writes one) -- fall back to opaque RGB.
        mode = "RGB" if img_arr.shape[2] >= 3 else "L"
        img = Image.fromarray(img_arr[:, :, :3] if img_arr.shape[2] >= 3 else img_arr[:, :, 0], mode)
    else:
        img = Image.fromarray(img_arr, "RGBA")

    preview_path = os.path.splitext(path)[0] + "_preview.png"
    img.save(preview_path)
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
    parser.add_argument(
        "--no-exposure-compensation", action="store_true",
        help="Disable the per-photo brightness equalization step (on by default).",
    )
    parser.add_argument(
        "--gcp", default=None,
        help="Optional CSV/XLSX file with surveyed Ground Control Points for specific "
             "photos (columns: photo, x/easting/lon, y/northing/lat), used for an "
             "iterative GCP/ICP position refinement. Leave unset to use GPS only.",
    )
    args = parser.parse_args()

    try:
        run_pipeline(
            args.input, args.output, args.pattern, args.max_photos, args.gsd,
            exposure_compensation=not args.no_exposure_compensation,
            gcp_path=args.gcp,
            feedback=print, progress=lambda p: None, is_canceled=lambda: False,
        )
    except (PipelineError, PipelineCanceled) as e:
        print(f"[FAILED] {e}")
        sys.exit(1)


if __name__ == "__main__":
    _main_cli()
