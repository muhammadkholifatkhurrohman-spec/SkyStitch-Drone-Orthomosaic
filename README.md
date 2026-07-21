# SkyStitch - Drone Orthomosaic — QGIS Plugin

A QGIS plugin to build an orthomosaic from raw drone photos (JPG + GPS EXIF)
directly inside QGIS, with no external software (WebODM/Pix4D/etc.) required.

The pipeline logic:
1. Read GPS (and, when available, gimbal/flight yaw) from each photo's EXIF/XMP
2. Feature matching (SIFT) between overlapping photos, run in parallel across CPU cores
3. Chain transforms (chain homography) into one shared pixel space
4. Correct scale/rotation/position using the real GPS positions (optionally refined
   further with surveyed GCP/ICP points — see below)
5. Render & blend: each photo is warped only into its own footprint (not the full
   canvas) and stitched using graph-cut seam finding by default, which cuts a clean
   line through overlaps instead of ghosting tall objects like rooftops or trees
   (a softer feathered blend is also available — see "Advanced options")
6. Save as GeoTIFF with a proper alpha channel, internal overviews, and your choice
   of compression — automatically loaded into the QGIS canvas

## 1. Install Python dependencies

A QGIS plugin uses QGIS's own Python environment (not your regular system
Python), so extra packages must be installed there:

**Windows (OSGeo4W / official QGIS installer):**
Open the **OSGeo4W Shell** (search for it in the Start Menu), then run:
```
python-qgis -m pip install opencv-python-headless exifread rasterio pyproj scipy pillow
```
(if `python-qgis` isn't recognized, try `python3 -m pip install ...` in the same shell)

**macOS (QGIS.app):**
```
/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install opencv-python-headless exifread rasterio pyproj scipy pillow
```

**Linux (QGIS installed via apt/system package, usually uses the system Python):**
```
python3 -m pip install --user opencv-python-headless exifread rasterio pyproj scipy pillow
```

After installing, **restart QGIS**.

**Optional:** if you want to use a **.xlsx (Excel)** GCP/ICP correction file
(see "GCP/ICP correction (optional)" below), also install `openpyxl` the
same way, e.g. `python-qgis -m pip install openpyxl`. Plain `.csv` GCP files
work with no extra dependency.

## 2. Install the plugin into QGIS

### Option A — Install from the QGIS Plugin Repository (once approved)
1. In QGIS, go to **Plugins → Manage and Install Plugins → All**
2. Search for **"SkyStitch"**
3. Click **Install Plugin**

### Option B — Manual install (for testing / before repository approval)
1. Find your QGIS profile folder: menu **Settings → User Profiles → Open Active Profile Folder**
2. Go to `python/plugins/` inside that folder
3. Copy the whole `skystitch` folder there, so the structure looks like:
   ```
   .../python/plugins/skystitch/
       __init__.py
       metadata.txt
       icon.png
       skystitch_plugin.py
       skystitch_dialog.py
       worker.py
       pipeline/
           core.py
           geo_utils.py
           feature_matching.py
           mosaic_builder.py
   ```
4. Open QGIS → menu **Plugins → Manage and Install Plugins → Installed**
5. Check **"SkyStitch - Drone Orthomosaic"** to enable it

The plugin will appear in:
- Menu **Raster → SkyStitch - Drone Orthomosaic**
- A toolbar icon (small 4-tile mosaic icon)

## 3. How to use

1. Click the plugin icon → the dialog opens
2. Select the **drone photo folder** (containing overlapping .jpg photos). Once
   photos are counted, the dialog shows a rough estimate of build time and peak
   RAM usage next to the photo count, before you even click "Build Mosaic"
3. Set the **output file (.tif)**
4. (optional) Limit the photo count first for a quick test before running the full batch
5. (optional) Set a **GCP / ICP file** — see "GCP/ICP correction (optional)" below
6. (optional) Tune **Advanced options** — see below — for blend mode, exposure
   compensation, and output compression
7. Click **"Build Mosaic"** — the process runs in the background, QGIS stays
   usable, progress & logs show which of the 7 pipeline steps is currently
   running next to the progress bar, and it can be canceled anytime via the
   **"Cancel"** button. Watch for `[WARNING]`/`[NOTICE]` messages in the message
   bar and log — e.g. an unrecognized camera model, a too-tight GPS baseline, or
   GPS altitudes varying by more than 15 m across the flight (a heuristic hint
   that the terrain may not be flat, since this plugin doesn't do DEM-based
   elevation correction)
8. When finished, the result is automatically loaded into the QGIS canvas
   (can be turned off via the checkbox). The dialog shows the final ground
   sample distance (m/px), resolution, and CRS as selectable/copyable text; no
   preview thumbnail is shown in the dialog itself — open the layer in the
   QGIS canvas (or the `_preview.png` saved next to the output .tif — the area
   outside the mosaic's tilted footprint is transparent, not black, since it
   carries the same alpha channel as the .tif) to inspect the result visually.

## Advanced options

- **Blend mode**: `seam` (default) finds a graph-cut line through each pair of
  overlapping photos and hard-assigns pixels to one side or the other, avoiding
  the "ghosting" double-exposure look on tall objects (rooftops, walls, trees).
  `feather` blends overlaps by distance-weighted averaging instead — fully
  seamless on flat, open terrain (farmland, open fields) with nothing tall to
  ghost, but slower-looking seams elsewhere and noticeably faster to render
  than `seam` in most cases.
- **Exposure/brightness compensation**: on by default; corrects for
  photo-to-photo exposure differences before blending. Can be turned off if you
  prefer the raw per-photo exposure.
- **Output compression**: choose between DEFLATE (previous default, lossless),
  ZSTD (lossless, usually smaller/faster than DEFLATE), LZW (lossless), JPEG
  (lossy, smallest files, adjustable quality 1–100%), or None (uncompressed).
  JPEG mode stores transparency as an internal GDAL mask band instead of a
  literal 4th band; QGIS and other GDAL-aware viewers still render it
  transparently the same way.

All advanced settings are remembered between sessions.

## GCP/ICP correction (optional)

By default, SkyStitch positions the mosaic using only each photo's GPS EXIF
(consumer-grade, typically +/-2-5 m accurate). If you have surveyed,
higher-accuracy coordinates for a few identifiable photos, you can supply
them in a **CSV or Excel (.xlsx)** file via the **"GCP / ICP file (optional)"**
field in Advanced options, and SkyStitch will use those photos as trusted
anchors — refined with an iterative (ICP-style) reweighting pass that also
down-weights GPS-derived positions that disagree strongly with the
GCP-anchored fit — to correct the whole mosaic's position/scale/rotation.

File format, one row per point, header required (column names are matched
case-insensitively; common aliases are accepted):

| photo         | x                     | y                      | z (optional)   |
|---------------|-----------------------|------------------------|----------------|
| filename/image/file/name | x/easting/east/lon/longitude | y/northing/north/lat/latitude | alt/altitude/elevation |

- `photo` must match one of the input photos **by filename only** (case-insensitive).
- Coordinates are auto-detected as lat/lon (WGS84) vs. already-projected
  meters based on magnitude, unless you add an optional `crs` column with a
  hint like `latlon`/`wgs84` or `utm`/`projected`.
- Not every photo needs a GCP row — only the photos you have surveyed
  coordinates for. The rest still use their GPS EXIF as before.
- A photo can be anchored by **more than one** GCP row (e.g. several
  surveyed points that both fall on/near the same photo). SkyStitch only
  ever anchors a whole photo, not individual pixels within it, so it
  averages multiple matching rows into one anchor position for that
  photo. If the rows disagree by more than 2 m after conversion, a
  `[WARNING]` is logged so a mistyped coordinate, wrong photo name, or
  wrong CRS gets noticed rather than silently averaged away.
- Leave the field empty to use GPS only (previous behavior, unchanged).

## Important limitations

- **Flat terrain only**: no elevation (DEM) correction is performed. For hilly/
  contoured areas or tall buildings, the result may be misaligned in those areas.
  As a lightweight heuristic, SkyStitch warns when photo GPS altitudes vary by
  more than 15 m across the flight (based on EXIF GPS altitude only), which can
  indicate hilly terrain or a mid-flight altitude change — this is a hint, not a
  DEM-based check.
- **Overlap is recommended, not required.** Photos overlapping by at least
  ~60-70% with their neighbors get proper feature-matched alignment. A photo
  that doesn't overlap with the rest is still placed on the canvas at its own
  GPS coordinate (no feature-matching refinement for that one), and a
  `[NOTICE]` in the log lists which photos this happened to -- it's not
  treated as an error and the build isn't stopped. If *no* photos overlap at
  all, every photo falls back to GPS-only placement the same way.
- **Performance**: hundreds of photos can take tens of minutes to hours. Test
  first with the "Limit photo count" option before running the full batch.
- **GPS baseline too tight**: automatically falls back to a GSD estimate from
  camera parameters.
- For the most accurate results at scale (hundreds/thousands of photos,
  survey-grade precision), WebODM (free) or Pix4D/DroneDeploy/Metashape (paid)
  are still recommended.

## Troubleshooting

**"Unexpected error: The EPSG code is unknown. PROJ: proj_create_from_database:
...\QGIS\share\proj\proj.db contains DATABASE.LAYOUT.VERSION.MINOR = 3 whereas
a number >= 4 is expected. It comes from another PROJ installation."**

This happened because QGIS sets its own `PROJ_LIB`/`PROJ_DATA` environment
variable at startup, pointing at QGIS's own (older) `proj.db`. That variable
is process-wide, so `rasterio` and `pyproj` (installed separately via pip)
inherited it too, even though they ship their own newer PROJ library that
needs a newer `proj.db` schema. This version fixes it (see
`pipeline/proj_fix.py`) by locating the `proj.db`/GDAL data folders bundled
*inside* the rasterio/pyproj wheels themselves and forcing both packages to
use those instead — no manual steps needed. If you still see this error after
updating, try restarting QGIS completely (not just re-running the tool), and
make sure you installed the dependencies into QGIS's own Python as described
in step 1 above (not a separate/system Python).

## Code structure

- `__init__.py` — required QGIS entry point (`classFactory`)
- `metadata.txt` — plugin info for the Plugin Manager
- `skystitch_plugin.py` — registers the menu/toolbar entry, opens the dialog
- `skystitch_dialog.py` — GUI (input form, log, progress bar)
- `worker.py` — runs the pipeline in a background thread (`QgsTask`) so QGIS doesn't freeze
- `pipeline/core.py` — the main pipeline logic, can also be run as a CLI:
  `python3 -m pipeline.core --input ... --output ...`
- `pipeline/geo_utils.py`, `pipeline/feature_matching.py`, `pipeline/mosaic_builder.py` —
  GPS/EXIF handling, SIFT feature matching, and mosaic rendering
- `pipeline/gcp_icp.py` — optional GCP CSV/XLSX loading + matching, used for the
  GCP/ICP position/scale/rotation refinement (see "GCP/ICP correction (optional)" above)

## QGIS Plugin Repository status

Published on the official [QGIS Plugin Repository](https://plugins.qgis.org/)
as a stable (non-experimental) plugin. See `metadata.txt`'s `changelog=` field
for the full version history.
