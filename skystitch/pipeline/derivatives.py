"""
derivatives.py
==============
Step 8 of the workflow ("Analysis & derivative products") done in QGIS,
straight from the DSM/DTM that the OpenDroneMap engine produced:

    * contour lines        (gdal:contour)
    * slope                (gdal:slope)
    * aspect               (gdal:aspect)
    * hillshade            (gdal:hillshade)
    * cut / fill           (difference of two DSMs + volume totals)

The raster-analysis products go through QGIS's own Processing framework
(the ``gdal:*`` algorithms), which is already initialised inside a running
QGIS session -- so results are written as real files and can be loaded as
layers. Cut/fill is done directly with GDAL + NumPy because it also needs
to return volume numbers, not just a raster.

Every function raises ``DerivativeError`` with a readable message on
failure and otherwise returns the output path (cut/fill returns a small
result dict). Import of QGIS/GDAL is done lazily inside each function so
this module still imports cleanly outside QGIS.
"""

import os


class DerivativeError(Exception):
    pass


def _run_processing(alg_id, params, feedback):
    """Thin wrapper around processing.run with a clear error if QGIS Processing
    isn't available (i.e. running outside QGIS)."""
    try:
        import processing  # available inside a running QGIS
    except Exception as e:
        raise DerivativeError(
            "QGIS Processing is not available in this Python environment; "
            "derivative products (contour/slope/hillshade) must be generated "
            f"from inside QGIS. ({e})"
        )
    feedback(f"[INFO] Running {alg_id} ...")
    try:
        return processing.run(alg_id, params)
    except Exception as e:
        raise DerivativeError(f"{alg_id} failed: {e}")


def generate_contour(dem_path, out_path, interval=1.0, base=0.0,
                     field_name="ELEV", feedback=lambda m: None):
    """Contour lines from a DEM/DSM. ``interval`` and ``base`` are in the DEM's
    vertical units (usually metres). Returns ``out_path``."""
    _require_file(dem_path, "DEM/DSM")
    res = _run_processing("gdal:contour", {
        "INPUT": dem_path,
        "BAND": 1,
        "INTERVAL": float(interval),
        "OFFSET": float(base),
        "FIELD_NAME": field_name,
        "CREATE_3D": False,
        "IGNORE_NODATA": False,
        "OUTPUT": out_path,
    }, feedback)
    out = res.get("OUTPUT", out_path)
    feedback(f"[INFO] Contours ({interval} unit interval) -> {out}")
    return out


def generate_slope(dem_path, out_path, as_percent=False, feedback=lambda m: None):
    """Slope raster (degrees by default). Returns ``out_path``."""
    _require_file(dem_path, "DEM/DSM")
    res = _run_processing("gdal:slope", {
        "INPUT": dem_path,
        "BAND": 1,
        "SCALE": 1.0,
        "AS_PERCENT": bool(as_percent),
        "COMPUTE_EDGES": True,
        "ZEVENBERGEN": False,
        "OPTIONS": "",
        "OUTPUT": out_path,
    }, feedback)
    out = res.get("OUTPUT", out_path)
    feedback(f"[INFO] Slope -> {out}")
    return out


def generate_aspect(dem_path, out_path, feedback=lambda m: None):
    """Aspect raster (compass direction the slope faces). Returns ``out_path``."""
    _require_file(dem_path, "DEM/DSM")
    res = _run_processing("gdal:aspect", {
        "INPUT": dem_path,
        "BAND": 1,
        "TRIG_ANGLE": False,
        "ZERO_FLAT": False,
        "COMPUTE_EDGES": True,
        "ZEVENBERGEN": False,
        "OPTIONS": "",
        "OUTPUT": out_path,
    }, feedback)
    out = res.get("OUTPUT", out_path)
    feedback(f"[INFO] Aspect -> {out}")
    return out


def generate_hillshade(dem_path, out_path, z_factor=1.0, azimuth=315.0,
                       altitude=45.0, feedback=lambda m: None):
    """Shaded-relief (hillshade) raster. Returns ``out_path``."""
    _require_file(dem_path, "DEM/DSM")
    res = _run_processing("gdal:hillshade", {
        "INPUT": dem_path,
        "BAND": 1,
        "Z_FACTOR": float(z_factor),
        "SCALE": 1.0,
        "AZIMUTH": float(azimuth),
        "ALTITUDE": float(altitude),
        "COMPUTE_EDGES": True,
        "ZEVENBERGEN": False,
        "COMBINED": False,
        "MULTIDIRECTIONAL": False,
        "OPTIONS": "",
        "OUTPUT": out_path,
    }, feedback)
    out = res.get("OUTPUT", out_path)
    feedback(f"[INFO] Hillshade -> {out}")
    return out


def cut_fill(dsm_before, dsm_after, out_path, feedback=lambda m: None):
    """Volume change between two surfaces on the same grid (e.g. before/after
    an earthworks job). Writes a difference raster (after - before) and returns
    a dict with the raster path plus cut/fill/net volumes in cubic metres.

    Positive difference = material added (fill); negative = material removed
    (cut). Requires the two rasters to share the same size and geotransform
    (as two ODM runs of the same area, at the same DEM resolution, will).
    """
    _require_file(dsm_before, "'before' DSM")
    _require_file(dsm_after, "'after' DSM")
    try:
        from osgeo import gdal
        import numpy as np
    except Exception as e:
        raise DerivativeError(f"GDAL/NumPy not available for cut/fill: {e}")

    gdal.UseExceptions()
    ds_b = gdal.Open(dsm_before)
    ds_a = gdal.Open(dsm_after)
    if ds_b is None or ds_a is None:
        raise DerivativeError("Could not open one of the DSM rasters.")

    if (ds_b.RasterXSize, ds_b.RasterYSize) != (ds_a.RasterXSize, ds_a.RasterYSize):
        raise DerivativeError(
            "The two DSMs have different pixel dimensions, so they can't be "
            "differenced directly. Re-run both areas at the same --dem-resolution, "
            "or clip/resample them to a common grid first."
        )

    gt = ds_a.GetGeoTransform()
    px_area = abs(gt[1] * gt[5])  # m^2 per pixel

    b_band, a_band = ds_b.GetRasterBand(1), ds_a.GetRasterBand(1)
    b = b_band.ReadAsArray().astype("float64")
    a = a_band.ReadAsArray().astype("float64")

    mask = np.ones(b.shape, dtype=bool)
    for band, arr in ((b_band, b), (a_band, a)):
        nod = band.GetNoDataValue()
        if nod is not None:
            mask &= (arr != nod)
    mask &= np.isfinite(a) & np.isfinite(b)

    diff = np.where(mask, a - b, 0.0)

    fill_vol = float(diff[diff > 0].sum() * px_area)          # material added
    cut_vol = float(-diff[diff < 0].sum() * px_area)          # material removed
    net_vol = fill_vol - cut_vol

    # write the difference raster (nodata outside the valid overlap)
    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(out_path, ds_a.RasterXSize, ds_a.RasterYSize, 1, gdal.GDT_Float32,
                           options=["COMPRESS=DEFLATE", "TILED=YES"])
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(ds_a.GetProjection())
    out_band = out_ds.GetRasterBand(1)
    out_arr = np.where(mask, a - b, -9999.0).astype("float32")
    out_band.WriteArray(out_arr)
    out_band.SetNoDataValue(-9999.0)
    out_band.FlushCache()
    out_ds = None
    ds_a = ds_b = None

    feedback(
        "[INFO] Cut/Fill (m^3): fill(+)=%.2f, cut(-)=%.2f, net=%.2f  ->  %s"
        % (fill_vol, cut_vol, net_vol, out_path)
    )
    return {
        "diff_raster": out_path,
        "fill_volume_m3": fill_vol,
        "cut_volume_m3": cut_vol,
        "net_volume_m3": net_vol,
        "pixel_area_m2": px_area,
    }


def _require_file(path, what):
    if not path or not os.path.isfile(path):
        raise DerivativeError(f"{what} file not found: {path}")
