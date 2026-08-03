"""
gcp_icp.py
==========
Optional Ground Control Point (GCP) support: lets the user provide a CSV
or Excel file with surveyed, high-accuracy coordinates for specific input
photos. Those photos are then used as trusted anchors -- together with an
iterative reweighting ("GCP/ICP") refinement in mosaic_builder.py -- to
correct the whole mosaic's absolute position/scale/rotation beyond what's
possible from consumer-GPS EXIF alone.

File format (CSV or .xlsx), one row per point, header required. Column
names are matched case-insensitively; a few common aliases are accepted:

    photo                      filename / image / file / name
    x   (easting or longitude) easting / east / lon / longitude
    y   (northing or latitude) northing / north / lat / latitude
    z   (optional)             alt / altitude / elevation
    crs (optional hint)        type / coord_type

`photo` must match one of the input photos by filename only (not full
path, case-insensitive). Coordinates are auto-detected as lat/lon (WGS84)
vs. already-projected meters based on their magnitude (|x| <= 180 and
|y| <= 90 => treated as longitude/latitude), unless a `crs` column
explicitly says something like "latlon"/"wgs84" or "utm"/"projected".
"""

import csv
import os

import numpy as np


_PHOTO_ALIASES = {"photo", "filename", "image", "file", "name"}
_X_ALIASES = {"x", "easting", "east", "lon", "longitude"}
_Y_ALIASES = {"y", "northing", "north", "lat", "latitude"}
_Z_ALIASES = {"z", "alt", "altitude", "elevation"}
_CRS_ALIASES = {"crs", "type", "coord_type"}

# How much more a surveyed GCP is trusted than an ordinary consumer-GPS
# photo position in the similarity fit -- not infinite, since a mistyped
# coordinate or a wrong filename match should still be visible/correctable
# rather than silently dominating the whole result.
GCP_WEIGHT = 25.0


class GcpError(Exception):
    """Raised when the GCP file can't be read or doesn't have usable columns."""


def _match_column(fieldnames, aliases):
    lower_map = {str(f).strip().lower(): f for f in fieldnames if f is not None}
    for alias in aliases:
        if alias in lower_map:
            return lower_map[alias]
    return None


def _rows_from_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise GcpError(f"'{os.path.basename(path)}' appears to be empty.")
        return list(reader.fieldnames), list(reader)


def _rows_from_xlsx(path):
    try:
        import openpyxl
    except ImportError as e:
        raise GcpError(
            "Reading .xlsx GCP files requires the 'openpyxl' package. Install it the same "
            "way as the other dependencies (see README.md), or export/save the GCP file as "
            ".csv instead."
        ) from e

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise GcpError(f"'{os.path.basename(path)}' appears to be empty.")
    header = [str(c).strip() if c is not None else "" for c in header_row]

    rows = []
    for values in rows_iter:
        if values is None or all(v is None for v in values):
            continue
        row = {header[i]: values[i] for i in range(min(len(header), len(values)))}
        rows.append(row)
    return header, rows


def load_gcp_points(path):
    """Read a GCP CSV/XLSX file. Returns a list of dicts:
    {"photo": str, "x": float, "y": float, "z": float or None, "is_latlon": bool}
    Raises GcpError with a user-friendly message on any problem."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        fieldnames, rows = _rows_from_xlsx(path)
    elif ext == ".csv":
        fieldnames, rows = _rows_from_csv(path)
    else:
        raise GcpError(f"Unsupported GCP file type '{ext}' (expected .csv or .xlsx).")

    photo_col = _match_column(fieldnames, _PHOTO_ALIASES)
    x_col = _match_column(fieldnames, _X_ALIASES)
    y_col = _match_column(fieldnames, _Y_ALIASES)
    z_col = _match_column(fieldnames, _Z_ALIASES)
    crs_col = _match_column(fieldnames, _CRS_ALIASES)

    if not photo_col or not x_col or not y_col:
        raise GcpError(
            "GCP file must have a photo/filename column and two coordinate columns "
            "(x/easting/longitude and y/northing/latitude). "
            f"Found columns: {', '.join(str(f) for f in fieldnames)}"
        )

    points = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        photo = row.get(photo_col)
        x_val = row.get(x_col)
        y_val = row.get(y_col)
        if photo in (None, "") or x_val in (None, "") or y_val in (None, ""):
            continue
        try:
            x = float(x_val)
            y = float(y_val)
        except (TypeError, ValueError):
            raise GcpError(f"Row {i}: could not read '{x_col}'/'{y_col}' as numbers.")

        z = None
        if z_col:
            z_val = row.get(z_col)
            if z_val not in (None, ""):
                try:
                    z = float(z_val)
                except (TypeError, ValueError):
                    z = None

        crs_hint = str(row.get(crs_col, "")).strip().lower() if crs_col else ""
        if "lat" in crs_hint or "wgs" in crs_hint or "4326" in crs_hint:
            is_latlon = True
        elif "utm" in crs_hint or "proj" in crs_hint or "meter" in crs_hint or "metre" in crs_hint:
            is_latlon = False
        else:
            is_latlon = abs(x) <= 180.0 and abs(y) <= 90.0

        points.append({
            "photo": os.path.basename(str(photo)).strip(),
            "x": x,
            "y": y,
            "z": z,
            "is_latlon": is_latlon,
        })

    if not points:
        raise GcpError("No usable GCP rows found (check the file has data below the header row).")

    return points


# If two or more GCP rows land on the same photo and their converted
# positions disagree by more than this many meters, it's more likely a
# data-entry mistake (wrong point, wrong photo name, wrong CRS) than
# ordinary survey noise -- worth a loud warning even though we still
# proceed by averaging.
GCP_MULTI_DISAGREEMENT_WARN_M = 2.0


def apply_gcp_corrections(photo_paths, world_xy, geo, gcp_points, feedback=print):
    """Match GCPs to photos by filename and build the (world_xy, weights,
    gcp_mask) inputs used by fit_world_similarity's GCP/ICP refinement.

    A single photo may be anchored by more than one GCP row (e.g. several
    surveyed points that all happen to fall within/near that photo's
    footprint); when that happens their converted world coordinates are
    simply averaged into one anchor position for that photo. This plugin
    only ever anchors *whole photos* (not individual pixels within a
    photo -- see the module docstring), so multiple GCPs on one photo
    cannot each pull a different part of the image; averaging is the
    correct way to fold them into that one-position-per-photo model.

    Returns: (world_xy_corrected, weights, gcp_mask, n_matched, unmatched_names)
      world_xy_corrected : world_xy with matched photos' positions replaced
                            by their surveyed GCP coordinates (converted to
                            the same UTM CRS as `geo`; averaged if a photo
                            had more than one matching GCP row)
      weights             : per-photo weight list (GCP_WEIGHT for matched
                             photos, 1.0 otherwise), same order as photo_paths
      gcp_mask             : per-photo boolean list, True for matched
                             (GCP-anchored) photos
      n_matched            : how many GCP rows were successfully matched
                             (can exceed the number of distinct anchored
                             photos, since several rows may share a photo)
      unmatched_names       : GCP photo names that didn't match any input photo
    """
    name_to_index = {}
    for i, p in enumerate(photo_paths):
        name_to_index.setdefault(os.path.basename(p).strip().lower(), i)

    # Collect every matched GCP's converted (x, y) per photo index first,
    # instead of writing straight into world_xy_corrected, so that a
    # second/third row for the same photo can be averaged in rather than
    # silently overwriting the previous one.
    xy_by_index = {}
    unmatched_names = []
    n_matched = 0

    for gcp in gcp_points:
        idx = name_to_index.get(gcp["photo"].lower())
        if idx is None:
            unmatched_names.append(gcp["photo"])
            continue

        if gcp["is_latlon"]:
            xy = geo.latlon_to_xy(gcp["x"], gcp["y"])
        else:
            xy = np.array([gcp["x"], gcp["y"]], dtype=np.float64)

        xy_by_index.setdefault(idx, []).append(xy)
        n_matched += 1

    world_xy_corrected = [np.asarray(xy, dtype=np.float64) for xy in world_xy]
    weights = [1.0] * len(photo_paths)
    gcp_mask = [False] * len(photo_paths)

    for idx, xy_list in xy_by_index.items():
        stacked = np.stack(xy_list, axis=0)
        averaged = stacked.mean(axis=0)

        if len(xy_list) > 1:
            spread = float(np.linalg.norm(stacked - averaged, axis=1).max())
            feedback(
                f"        {len(xy_list)} GCP rows matched to '{os.path.basename(photo_paths[idx])}' "
                f"-- averaged into one anchor (max deviation from average: {spread:.2f} m)."
            )
            if spread > GCP_MULTI_DISAGREEMENT_WARN_M:
                feedback(
                    f"[WARNING] GCP rows for '{os.path.basename(photo_paths[idx])}' disagree by up "
                    f"to {spread:.2f} m -- double-check these aren't different points, a wrong photo "
                    f"name, or a wrong CRS before trusting the averaged result."
                )

        world_xy_corrected[idx] = averaged
        weights[idx] = GCP_WEIGHT
        gcp_mask[idx] = True

    if unmatched_names:
        shown = unmatched_names[:10]
        suffix = ", ..." if len(unmatched_names) > 10 else ""
        feedback(
            f"[WARNING] {len(unmatched_names)} GCP row(s) didn't match any input photo by "
            f"filename and were ignored: {shown}{suffix}"
        )

    return world_xy_corrected, weights, gcp_mask, n_matched, unmatched_names
