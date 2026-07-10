"""
skystitch_plugin.py
====================
Main plugin class: registers the menu entry & toolbar button in QGIS,
and opens the main dialog when clicked.
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class SkyStitchPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(QIcon(icon_path), "SkyStitch - Drone Orthomosaic", self.iface.mainWindow())
        self.action.setWhatsThis("Build an orthomosaic from raw drone photos (JPG + GPS EXIF)")
        self.action.setStatusTip("Build an orthomosaic from raw drone photos")
        self.action.triggered.connect(self.run)

        # toolbar button + entry in the Raster menu
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu("SkyStitch - Drone Orthomosaic", self.action)

    def unload(self):
        self.iface.removePluginRasterMenu("SkyStitch - Drone Orthomosaic", self.action)
        self.iface.removeToolBarIcon(self.action)
        self.action = None
        self.dialog = None

    def run(self):
        # import here (not at module top-level) so that if a dependency
        # (opencv/rasterio/etc.) isn't installed yet, the error only shows
        # up when the button is clicked -- instead of breaking QGIS startup
        # or making the plugin fail to load.
        try:
            from .skystitch_dialog import SkyStitchDialog
        except ImportError as e:
            from qgis.PyQt.QtWidgets import QMessageBox

            QMessageBox.critical(
                self.iface.mainWindow(),
                "SkyStitch - Missing dependencies",
                "This plugin requires a few extra Python packages that are not yet "
                "installed in QGIS's Python environment:\n\n"
                "opencv-python-headless, exifread, rasterio, pyproj, scipy, pillow\n\n"
                f"Error details: {e}\n\n"
                "See README.md in the plugin folder for installation instructions.",
            )
            return

        if self.dialog is None:
            self.dialog = SkyStitchDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
