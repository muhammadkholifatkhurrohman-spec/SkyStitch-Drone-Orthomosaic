"""
geo_utils.py
============
Utilities for reading GPS position from drone photo EXIF, and for
converting GPS coordinates (lat/lon) <-> local meter coordinates (ENU -
East North Up) for use in geometric calculations (feature matching, etc).
"""

from . import proj_fix  # noqa: F401 - must run before pyproj touches PROJ

import logging

import exifread
import numpy as np
import pyproj

_log = logging.getLogger(__name__)


def _to_deg(value):
    """Convert an EXIF rational value (deg, min, sec) to decimal degrees."""
    d, m, s = value.values
    return float(d.num) / d.den + (float(m.num) / m.den) / 60.0 + (float(s.num) / s.den) / 3600.0


def read_gps(image_path):
    """Read lat, lon (decimal degrees), altitude (meters) from a photo's EXIF.
    Returns None if the photo has no GPS data."""
    with open(image_path, "rb") as f:
        tags = exifread.process_file(f, details=False)

    if "GPS GPSLatitude" not in tags or "GPS GPSLongitude" not in tags:
        return None

    lat = _to_deg(tags["GPS GPSLatitude"])
    if tags.get("GPS GPSLatitudeRef", None) and str(tags["GPS GPSLatitudeRef"]) == "S":
        lat = -lat

    lon = _to_deg(tags["GPS GPSLongitude"])
    if tags.get("GPS GPSLongitudeRef", None) and str(tags["GPS GPSLongitudeRef"]) == "W":
        lon = -lon

    alt = 0.0
    if "GPS GPSAltitude" in tags:
        v = tags["GPS GPSAltitude"].values[0]
        alt = float(v.num) / v.den
        if "GPS GPSAltitudeRef" in tags and int(str(tags["GPS GPSAltitudeRef"])) == 1:
            alt = -alt

    yaw = None
    # some DJI firmware stores gimbal yaw in XMP, not standard EXIF.
    # exifread doesn't read XMP, so we try a manual fallback from raw bytes below.
    return {"lat": lat, "lon": lon, "alt": alt, "yaw": yaw}


def read_xmp_gimbal_yaw(image_path):
    """DJI stores gimbal orientation (yaw/pitch/roll) in an XMP block inside
    the JPG. We search for the XMP string manually and pull out
    GimbalYawDegree if present."""
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        start = data.find(b"<x:xmpmeta")
        end = data.find(b"</x:xmpmeta>")
        if start == -1 or end == -1:
            return None
        xmp = data[start:end].decode("utf-8", errors="ignore")
        import re

        m = re.search(r'GimbalYawDegree="([-\d.]+)"', xmp)
        if m:
            return float(m.group(1))
        m = re.search(r'FlightYawDegree="([-\d.]+)"', xmp)
        if m:
            return float(m.group(1))
    except Exception:
        # Best-effort only: this XMP block is optional (most non-DJI
        # cameras never have one), so a read/decode failure here should
        # never abort georeferencing -- just fall back to no gimbal yaw
        # (root_yaw_deg=None) the same as if the block was simply absent.
        # Logged at debug level so a genuine bug (as opposed to the
        # expected "no XMP here") is still discoverable, without being
        # noisy for the common case.
        _log.debug("Failed to read XMP gimbal yaw from '%s'", image_path, exc_info=True)
    return None


SENSOR_WIDTH_MM = {
    ("DJI", "FC220"): 6.3,
    ("DJI", "FC330"): 6.3,
    ("DJI", "FC6310"): 13.2,
    ("DJI", "FC7203"): 6.3,
    ("DJI", "FC3170"): 6.3,
    ("DJI", "FC3411"): 17.3,
    ("Hasselblad", "L1D-20c"): 17.3,
}
DEFAULT_SENSOR_WIDTH_MM = 6.3


def read_camera_params(image_path):
    with open(image_path, "rb") as f:
        tags = exifread.process_file(f, details=False)

    focal_mm = None
    if "EXIF FocalLength" in tags:
        v = tags["EXIF FocalLength"].values[0]
        focal_mm = float(v.num) / v.den

    width_px = None
    if "EXIF ExifImageWidth" in tags:
        width_px = int(str(tags["EXIF ExifImageWidth"]))
    elif "Image ImageWidth" in tags:
        width_px = int(str(tags["Image ImageWidth"]))

    make = str(tags.get("Image Make", "")).strip()
    model = str(tags.get("Image Model", "")).strip()
    sensor_known = (make, model) in SENSOR_WIDTH_MM
    sensor_mm = SENSOR_WIDTH_MM.get((make, model), DEFAULT_SENSOR_WIDTH_MM)

    return {
        "focal_mm": focal_mm,
        "width_px": width_px,
        "sensor_mm": sensor_mm,
        "sensor_known": sensor_known,
        "make": make,
        "model": model,
    }


def estimate_analytic_gsd(image_path, altitude_m):
    """Estimate meters/pixel from camera parameters (sensor width, focal
    length, image width) and flight altitude. Returns a dict:
      {"gsd": float or None, "sensor_known": bool, "make": str, "model": str}
    "gsd" is None if there isn't enough EXIF data to compute it (missing
    focal length, missing image width, or non-positive altitude).
    "sensor_known" is False when (make, model) isn't in SENSOR_WIDTH_MM, in
    which case DEFAULT_SENSOR_WIDTH_MM was used as a guess -- callers
    should surface this to the user instead of silently trusting the
    result, since an unknown sensor width can make the estimate
    meaningfully wrong."""
    p = read_camera_params(image_path)
    gsd = None
    if p["focal_mm"] and p["width_px"] and altitude_m > 0:
        gsd = (p["sensor_mm"] * altitude_m) / (p["focal_mm"] * p["width_px"])
    return {"gsd": gsd, "sensor_known": p["sensor_known"], "make": p["make"], "model": p["model"]}


def choose_utm_crs(lon, lat):
    """Automatically choose a UTM zone EPSG code based on location (for
    accurate meter units)."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


# Below this altitude spread (meters), differences are more likely to be
# ordinary consumer-GPS noise (typically +/-3-5m) than a real signal.
TERRAIN_RELIEF_WARN_M = 15.0


def assess_terrain_relief(gps_list):
    """Cheap heuristic heads-up for non-flat terrain, using ONLY the GPS
    altitude already read from EXIF (no DEM/elevation data is available or
    used anywhere in this plugin -- see the "Important limitations" section
    in README.md).

    Photo GPS altitude is the drone's absolute altitude (MSL or similar),
    not its height above the ground directly below it, so this can't
    measure terrain relief directly. But a wide spread of altitudes across
    the flight is still a useful (if imperfect) signal: it happens either
    because the drone was flying in terrain-following mode (altitude
    tracks the ground, so a wide spread really does mean hilly terrain),
    or because the flight altitude was changed mid-flight for some other
    reason (which independently changes each photo's ground sample
    distance and can make photos harder to align consistently). Either
    way, it's worth flagging given this plugin assumes one flat ground
    plane and does no per-pixel elevation correction.

    Returns a warning message string, or None if nothing stands out.
    """
    altitudes = [g["alt"] for g in gps_list if g.get("alt")]
    if len(altitudes) < 2:
        return None

    alt_range = max(altitudes) - min(altitudes)
    if alt_range <= TERRAIN_RELIEF_WARN_M:
        return None

    return (
        f"Photo altitudes span {alt_range:.1f} m across the flight (from "
        f"{min(altitudes):.1f} m to {max(altitudes):.1f} m). This can mean the "
        f"terrain isn't flat (e.g. hills, slopes) or that the flight altitude "
        f"changed mid-flight -- this plugin doesn't perform DEM-based elevation "
        f"correction, so parts of the result may be misaligned. If the terrain "
        f"is genuinely hilly or has tall structures, consider a dedicated "
        f"photogrammetry tool (WebODM, Pix4D, DroneDeploy, Metashape) instead."
    )


class GeoConverter:
    """Convert lat/lon <-> local meter coordinates (UTM projection) for one working area."""

    def __init__(self, ref_lon, ref_lat):
        self.utm_crs = choose_utm_crs(ref_lon, ref_lat)
        self.to_utm = pyproj.Transformer.from_crs("EPSG:4326", self.utm_crs, always_xy=True)
        self.to_wgs84 = pyproj.Transformer.from_crs(self.utm_crs, "EPSG:4326", always_xy=True)

    def latlon_to_xy(self, lon, lat):
        x, y = self.to_utm.transform(lon, lat)
        return np.array([x, y])

    def xy_to_latlon(self, x, y):
        lon, lat = self.to_wgs84.transform(x, y)
        return lon, lat
