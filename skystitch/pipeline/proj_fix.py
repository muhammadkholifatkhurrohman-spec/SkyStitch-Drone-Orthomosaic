"""
proj_fix.py
===========
On Windows, QGIS sets its own PROJ_LIB / PROJ_DATA environment variable at
startup, pointing at QGIS's own bundled proj.db (often an older schema
version). That environment variable is process-wide, so it also gets
inherited by pyproj and rasterio (installed separately via pip for this
plugin), even though those packages ship their OWN, newer PROJ library
that expects a newer proj.db schema.

That mismatch is exactly what produces errors like:

    Unexpected error: The EPSG code is unknown. PROJ:
    proj_create_from_database: ...\\QGIS\\share\\proj\\proj.db contains
    DATABASE.LAYOUT.VERSION.MINOR = 3 whereas a number >= 4 is expected.
    It comes from another PROJ installation.

WHY THE FIRST VERSION OF THIS FIX DIDN'T WORK
----------------------------------------------
The previous approach did:

    os.environ["PROJ_DATA"] = pyproj.datadir.get_data_dir()

but `pyproj.datadir.get_data_dir()` itself checks the existing PROJ_DATA /
PROJ_LIB environment variables FIRST, and simply returns that path back if
it looks like a valid proj data folder (i.e. it contains *a* proj.db,
regardless of schema version). Since QGIS had already set that env var to
its own (older) proj.db before our code ever ran, `get_data_dir()` just
handed us QGIS's directory right back -- a no-op. rasterio's own bundled
GDAL/PROJ then loaded its (newer) proj.dll, pointed it at QGIS's (older)
proj.db via that same env var, and failed with the schema-version error
above, specifically while writing the GeoTIFF (which is the first place
rasterio/GDAL actually needs to resolve an EPSG code).

THE FIX
-------
Ignore whatever PROJ_LIB / PROJ_DATA / GDAL_DATA are already set to, and
explicitly locate the proj/GDAL data folders that ship *inside* the
rasterio and pyproj wheels themselves, then:
  1. point the environment variables at those wheel-bundled folders, and
  2. call `pyproj.datadir.set_data_dir()` directly, which overrides
     pyproj's internal cache regardless of env vars, and
  3. expose `rasterio_env()`, a `rasterio.Env(...)` context manager that
     re-asserts the same paths at the exact moment GDAL/PROJ is used
     (`rasterio.Env` calls CPLSetConfigOption(), which -- unlike plain
     os.environ assignment -- takes effect immediately even if GDAL had
     already cached a stale value from QGIS's startup).

IMPORTANT: this module must be imported *before* `import rasterio` (and
before creating any pyproj object) anywhere in the plugin, and any code
that opens/writes a raster with a CRS should do so inside `rasterio_env()`.
"""

import os


def _valid_proj_dir(path):
    return bool(path) and os.path.isfile(os.path.join(path, "proj.db"))


def _rasterio_wheel_dirs():
    """Return (gdal_data_dir, proj_data_dir) bundled inside the rasterio
    wheel itself, ignoring any pre-existing GDAL_DATA/PROJ_LIB/PROJ_DATA
    environment variables. Returns (None, None) if rasterio isn't
    installed or doesn't ship its own bundled data (e.g. a non-wheel
    install)."""
    try:
        from rasterio.env import GDALDataFinder, PROJDataFinder
    except Exception:
        return None, None

    gdal_dir = None
    try:
        gdal_dir = GDALDataFinder().search_wheel()
    except Exception:
        gdal_dir = None

    proj_dir = None
    try:
        proj_dir = PROJDataFinder().search_wheel()
    except Exception:
        proj_dir = None

    if not _valid_proj_dir(proj_dir):
        proj_dir = None

    return gdal_dir, proj_dir


def _pyproj_wheel_dir():
    """Return pyproj's OWN bundled proj data directory, bypassing
    pyproj.datadir.get_data_dir()'s "trust an existing env var first"
    behaviour (which is exactly what let QGIS's older proj.db leak in)."""
    try:
        import pyproj
    except Exception:
        return None

    pkg_dir = os.path.dirname(pyproj.__file__)
    for candidate in (
        os.path.join(pkg_dir, "proj_dir", "share", "proj"),
        os.path.join(pkg_dir, "data", "proj"),
    ):
        if _valid_proj_dir(candidate):
            return candidate
    return None


_gdal_dir, _proj_dir = _rasterio_wheel_dirs()
if not _proj_dir:
    _proj_dir = _pyproj_wheel_dir()

if _proj_dir:
    os.environ["PROJ_DATA"] = _proj_dir
    os.environ["PROJ_LIB"] = _proj_dir  # older PROJ versions look for this name

    # Force pyproj to use this exact directory too, instead of re-deriving
    # it (and potentially picking QGIS's directory again) the next time
    # something asks.
    try:
        import pyproj.datadir

        pyproj.datadir.set_data_dir(_proj_dir)
    except Exception:
        pass

if _gdal_dir:
    os.environ["GDAL_DATA"] = _gdal_dir


def rasterio_env():
    """Context manager that re-asserts the correct GDAL_DATA/PROJ_LIB/
    PROJ_DATA paths at the exact moment rasterio touches GDAL (opening or
    writing a dataset, resolving a CRS, etc). Use this around any
    `rasterio.open(...)` call that sets/reads a CRS:

        with rasterio_env():
            with rasterio.open(path, "w", crs=crs, ...) as dst:
                ...

    Unlike a plain os.environ assignment, `rasterio.Env(...)` calls GDAL's
    CPLSetConfigOption() directly, so it takes effect even if GDAL had
    already cached a stale value from QGIS's own startup.
    """
    import rasterio

    kwargs = {}
    if _gdal_dir:
        kwargs["GDAL_DATA"] = _gdal_dir
    if _proj_dir:
        kwargs["PROJ_LIB"] = _proj_dir
        kwargs["PROJ_DATA"] = _proj_dir
    return rasterio.Env(**kwargs)
