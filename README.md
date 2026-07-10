SkyStitch - Drone Orthomosaic — QGIS Plugin
A QGIS plugin to build an orthomosaic from raw drone photos (JPG + GPS EXIF)
directly inside QGIS, with no external software (WebODM/Pix4D/etc.) required.
The pipeline logic:
Read GPS from each photo's EXIF
Feature matching (SIFT) between overlapping photos
Chain transforms (chain homography) into one shared pixel space
Correct scale/rotation/position using the real GPS positions
Render & blend (warp all photos onto one canvas, feathering the overlaps)
Save as GeoTIFF — automatically loaded into the QGIS canvas
1. Install Python dependencies
A QGIS plugin uses QGIS's own Python environment (not your regular system
Python), so extra packages must be installed there:
Windows (OSGeo4W / official QGIS installer):
Open the OSGeo4W Shell (search for it in the Start Menu), then run:
```
python-qgis -m pip install opencv-python-headless exifread rasterio pyproj scipy pillow
```
(if `python-qgis` isn't recognized, try `python3 -m pip install ...` in the same shell)
macOS (QGIS.app):
```
/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install opencv-python-headless exifread rasterio pyproj scipy pillow
```
Linux (QGIS installed via apt/system package, usually uses the system Python):
```
python3 -m pip install --user opencv-python-headless exifread rasterio pyproj scipy pillow
```
After installing, restart QGIS.
2. Install the plugin into QGIS
Option A — Install from the QGIS Plugin Repository (once approved)
In QGIS, go to Plugins → Manage and Install Plugins → All
Search for "SkyStitch"
Click Install Plugin
Option B — Manual install (for testing / before repository approval)
Find your QGIS profile folder: menu Settings → User Profiles → Open Active Profile Folder
Go to `python/plugins/` inside that folder
Copy the whole `skystitch` folder there, so the structure looks like:
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
Open QGIS → menu Plugins → Manage and Install Plugins → Installed
Check "SkyStitch - Drone Orthomosaic" to enable it
The plugin will appear in:
Menu Raster → SkyStitch - Drone Orthomosaic
A toolbar icon (small 4-tile mosaic icon)
3. How to use
Click the plugin icon → the dialog opens
Select the drone photo folder (containing overlapping .jpg photos)
Set the output file (.tif)
(optional) Limit the photo count first for a quick test before running the full batch
Click "Build Mosaic" — the process runs in the background, QGIS stays
usable, progress & logs show in the dialog, and it can be canceled anytime via
the "Cancel" button
When finished, the result is automatically loaded into the QGIS canvas
(can be turned off via the checkbox)
Important limitations
Flat terrain only: no elevation (DEM) correction is performed. For hilly/
contoured areas or tall buildings, the result may be misaligned in those areas.
Photos must overlap by at least ~60-70% with their neighboring photos.
Performance: hundreds of photos can take tens of minutes to hours. Test
first with the "Limit photo count" option before running the full batch.
GPS baseline too tight: automatically falls back to a GSD estimate from
camera parameters.
For the most accurate results at scale (hundreds/thousands of photos,
survey-grade precision), WebODM (free) or Pix4D/DroneDeploy/Metashape (paid)
are still recommended.
Troubleshooting
"Unexpected error: The EPSG code is unknown. PROJ: proj_create_from_database:
...\QGIS\share\proj\proj.db contains DATABASE.LAYOUT.VERSION.MINOR = 3 whereas
a number >= 4 is expected. It comes from another PROJ installation."
This happened because QGIS sets its own `PROJ_LIB`/`PROJ_DATA` environment
variable at startup, pointing at QGIS's own (older) `proj.db`. That variable
is process-wide, so `rasterio` and `pyproj` (installed separately via pip)
inherited it too, even though they ship their own newer PROJ library that
needs a newer `proj.db` schema. This version fixes it (see
`pipeline/proj_fix.py`) by locating the `proj.db`/GDAL data folders bundled
inside the rasterio/pyproj wheels themselves and forcing both packages to
use those instead — no manual steps needed. If you still see this error after
updating, try restarting QGIS completely (not just re-running the tool), and
make sure you installed the dependencies into QGIS's own Python as described
in step 1 above (not a separate/system Python).
Code structure
`__init__.py` — required QGIS entry point (`classFactory`)
`metadata.txt` — plugin info for the Plugin Manager
`skystitch_plugin.py` — registers the menu/toolbar entry, opens the dialog
`skystitch_dialog.py` — GUI (input form, log, progress bar)
`worker.py` — runs the pipeline in a background thread (`QgsTask`) so QGIS doesn't freeze
`pipeline/core.py` — the main pipeline logic, can also be run as a CLI:
`python3 -m pipeline.core --input ... --output ...`
`pipeline/geo_utils.py`, `pipeline/feature_matching.py`, `pipeline/mosaic_builder.py` —
GPS/EXIF handling, SIFT feature matching, and mosaic rendering
Contributing
Issues and pull requests are welcome — please use the issue tracker.
License
Released under the GPL-2.0-or-later License.
Author
Muhammad Kholifatkhur Rohman
