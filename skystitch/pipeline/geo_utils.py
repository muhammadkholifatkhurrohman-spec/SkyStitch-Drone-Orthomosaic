"""
geo_utils.py
============
Utilities for reading GPS position from drone photo EXIF, and for
converting GPS coordinates (lat/lon) <-> local meter coordinates (ENU -
East North Up) for use in geometric calculations (feature matching, etc).
"""

from . import proj_fix  # noqa: F401 - must run before pyproj touches PROJ

import exifread
import numpy as np
import pyproj


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
        pass
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
    sensor_mm = SENSOR_WIDTH_MM.get((make, model), DEFAULT_SENSOR_WIDTH_MM)

    return {"focal_mm": focal_mm, "width_px": width_px, "sensor_mm": sensor_mm, "make": make, "model": model}


def estimate_analytic_gsd(image_path, altitude_m):
    p = read_camera_params(image_path)
    if not p["focal_mm"] or not p["width_px"] or altitude_m <= 0:
        return None
    return (p["sensor_mm"] * altitude_m) / (p["focal_mm"] * p["width_px"])


def choose_utm_crs(lon, lat):
    """Automatically choose a UTM zone EPSG code based on location (for
    accurate meter units)."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


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
