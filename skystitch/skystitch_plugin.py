"""
skystitch_plugin.py
====================
Registers the SkyStitch menu entries & toolbar buttons in QGIS.

Default menu (Raster -> SkyStitch):
  * "SkyStitch - Drone Orthomosaic (2D)"                 -> fast 2D stitch, no engine
  * "SkyStitch - 3D Photogrammetry (local node)"         -> full 3D on a locally
        installed NodeODM node (e.g. the free WebODM native installer's node).
        Each user installs the engine once; the plugin talks to it directly.

Two extra engines ship but are hidden by default (flip the flags to show them):
  * WebODM (remote, login)  -> SHOW_WEBODM_REMOTE_ENGINE
  * local OpenDroneMap (Docker) -> SHOW_LOCAL_DOCKER_ENGINE
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

MENU = "SkyStitch - Drone Orthomosaic"

# Optional engines, hidden by default to keep the menu simple.
SHOW_WEBODM_REMOTE_ENGINE = False   # WebODM web-app (login) engine
SHOW_LOCAL_DOCKER_ENGINE = False    # local OpenDroneMap via Docker

LABEL_2D = "SkyStitch - Drone Orthomosaic (2D)"
LABEL_NODE = "SkyStitch - 3D Photogrammetry (local node)"
LABEL_WEBODM = "SkyStitch - 3D via WebODM (remote/login)"
LABEL_DOCKER = "SkyStitch - 3D via local Docker (advanced)"


class SkyStitchPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.node_action = None
        self.webodm_action = None
        self.odm_action = None
        self.dialog = None
        self.node_dialog = None
        self.webodm_dialog = None
        self.odm_dialog = None

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.png"))

        # 1) fast 2D stitch (no engine)
        self.action = QAction(icon, LABEL_2D, self.iface.mainWindow())
        self.action.setStatusTip("Build a fast 2D orthomosaic from raw drone photos (no engine needed)")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(MENU, self.action)

        # 2) full 3D on a local NodeODM node (the main 3D path)
        self.node_action = QAction(icon, LABEL_NODE, self.iface.mainWindow())
        self.node_action.setStatusTip("Full 3D on a local NodeODM node -> ortho, DSM, DTM, point cloud, mesh (install the engine once)")
        self.node_action.triggered.connect(self.run_node)
        self.iface.addToolBarIcon(self.node_action)
        self.iface.addPluginToRasterMenu(MENU, self.node_action)

        # 3) (hidden) WebODM web-app engine
        if SHOW_WEBODM_REMOTE_ENGINE:
            self.webodm_action = QAction(icon, LABEL_WEBODM, self.iface.mainWindow())
            self.webodm_action.triggered.connect(self.run_webodm)
            self.iface.addToolBarIcon(self.webodm_action)
            self.iface.addPluginToRasterMenu(MENU, self.webodm_action)

        # 4) (hidden) local OpenDroneMap via Docker
        if SHOW_LOCAL_DOCKER_ENGINE:
            self.odm_action = QAction(icon, LABEL_DOCKER, self.iface.mainWindow())
            self.odm_action.triggered.connect(self.run_odm)
            self.iface.addToolBarIcon(self.odm_action)
            self.iface.addPluginToRasterMenu(MENU, self.odm_action)

    def unload(self):
        for act in (self.action, self.node_action, self.webodm_action, self.odm_action):
            if act is not None:
                self.iface.removePluginRasterMenu(MENU, act)
                self.iface.removeToolBarIcon(act)
        self.action = self.node_action = self.webodm_action = self.odm_action = None
        self.dialog = self.node_dialog = self.webodm_dialog = self.odm_dialog = None

    # -- 2D engine --
    def run(self):
        try:
            from .skystitch_dialog import SkyStitchDialog
        except ImportError as e:
            self._missing_deps("opencv-python-headless, exifread, rasterio, pyproj, scipy, pillow", e)
            return
        if self.dialog is None:
            self.dialog = SkyStitchDialog(self.iface, self.iface.mainWindow())
        self._show(self.dialog)

    # -- 3D via local NodeODM node --
    def run_node(self):
        try:
            from .nodeodm_dialog import NodeODMDialog
        except ImportError as e:
            self._missing_deps("requests", e)
            return
        if self.node_dialog is None:
            self.node_dialog = NodeODMDialog(self.iface, self.iface.mainWindow())
        self._show(self.node_dialog)

    # -- 3D via WebODM web app (hidden) --
    def run_webodm(self):
        try:
            from .webodm_dialog import WebODMDialog
        except ImportError as e:
            self._missing_deps("requests", e)
            return
        if self.webodm_dialog is None:
            self.webodm_dialog = WebODMDialog(self.iface, self.iface.mainWindow())
        self._show(self.webodm_dialog)

    # -- 3D via local Docker ODM (hidden) --
    def run_odm(self):
        try:
            from .odm_dialog import OdmDialog
        except ImportError as e:
            self._missing_deps("(none beyond QGIS itself)", e)
            return
        if self.odm_dialog is None:
            self.odm_dialog = OdmDialog(self.iface, self.iface.mainWindow())
        self._show(self.odm_dialog)

    def _show(self, dlg):
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _missing_deps(self, packages, err):
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.critical(
            self.iface.mainWindow(),
            "SkyStitch - Missing dependencies",
            "This tool requires extra Python packages that are not yet installed in "
            "QGIS's Python environment:\n\n"
            "%s\n\n"
            "Error details: %s\n\n"
            "See README.md in the plugin folder for installation instructions." % (packages, err),
        )
