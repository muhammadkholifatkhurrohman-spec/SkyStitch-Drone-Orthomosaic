"""
nodeodm_dialog.py
=================
Dialog for SkyStitch's local 3D engine: connect straight to a NodeODM node
running on this machine (e.g. the WebODM native installer's processing node,
or a standalone NodeODM), upload photos, process, and load the results into
QGIS. No WebODM login, no Docker on the user's side beyond having the node.

Same UI conventions as the other dialogs (scrollable form; pinned log/
progress/buttons; background QgsTask + signals).
"""

import logging
import os
import re
import time

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QCheckBox, QPushButton, QPlainTextEdit, QProgressBar,
    QMessageBox, QGroupBox, QLineEdit, QScrollArea, QWidget, QFrame,
)

from .nodeodm_worker import NodeODMTask
from .pipeline.nodeodm_client import NodeODMClient, find_images, requests_available

SETTINGS_GROUP = "SkyStitchNodeODM"
PC_QUALITY = [
    ("Medium (balanced, default)", "medium"),
    ("High (denser, slower)", "high"),
    ("Ultra (densest, slowest)", "ultra"),
    ("Low (faster)", "low"),
    ("Lowest (fastest, roughest)", "lowest"),
]
_STEP_RE = re.compile(r"^\[STEP (\d+)/(\d+)\]\s*(.+)$")


class NodeODMDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.task = None
        self.start_time = None
        self.last_output_dir = None

        self.setWindowTitle("SkyStitch - 3D Photogrammetry (local node)")
        self.setMinimumWidth(640)
        self.setMinimumHeight(460)
        self.resize(680, 720)
        self._build_ui()
        self._restore_settings()
        self._update_photo_count()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)

        info = QLabel(
            "Full 3D photogrammetry on a local processing node (NodeODM). Install the "
            "engine once (e.g. the free WebODM native installer, or a NodeODM), then "
            "point this at it -- everything else happens from QGIS, no Docker to run.\n"
            "Tip: the WebODM app's node URL is under Processing Nodes (e.g. http://localhost:3000)."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # ---- node ----
        node_box = QGroupBox("Processing node (NodeODM)")
        nf = QFormLayout(node_box)
        nf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.url_edit = QLineEdit("http://localhost:3000")
        self.url_edit.setToolTip("Address of the local NodeODM node. WebODM's native node is shown under Processing Nodes.")
        nf.addRow("Node URL:", self.url_edit)
        self.token_edit = QLineEdit()
        self.token_edit.setPlaceholderText("(leave empty if the node has no token)")
        nf.addRow("Token (optional):", self.token_edit)
        trow = QHBoxLayout()
        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self._on_test_connection)
        trow.addWidget(self.test_btn)
        trow.addStretch()
        nf.addRow("", trow)
        layout.addWidget(node_box)

        # ---- input/output ----
        io_box = QGroupBox("Input / Output")
        io = QFormLayout(io_box)
        io.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.input_widget = QgsFileWidget()
        self.input_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.input_widget.setDialogTitle("Select the folder containing the drone photos")
        self.input_widget.fileChanged.connect(self._update_photo_count)
        io.addRow("Drone photo folder:", self.input_widget)
        self.photo_count_label = QLabel("")
        self.photo_count_label.setStyleSheet("color: palette(mid);")
        io.addRow("", self.photo_count_label)
        self.output_widget = QgsFileWidget()
        self.output_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.output_widget.setDialogTitle("Folder to save the downloaded results")
        io.addRow("Output folder:", self.output_widget)
        layout.addWidget(io_box)

        # ---- options ----
        opt_box = QGroupBox("Processing options")
        opt = QFormLayout(opt_box)
        opt.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.pc_combo = QComboBox()
        for label, data in PC_QUALITY:
            self.pc_combo.addItem(label, data)
        opt.addRow("Point cloud quality:", self.pc_combo)
        self.ortho_res_spin = QDoubleSpinBox()
        self.ortho_res_spin.setRange(0.1, 100.0)
        self.ortho_res_spin.setDecimals(1)
        self.ortho_res_spin.setValue(5.0)
        self.ortho_res_spin.setSuffix(" cm/px")
        opt.addRow("Orthophoto resolution:", self.ortho_res_spin)
        self.dem_res_spin = QDoubleSpinBox()
        self.dem_res_spin.setRange(0.0, 100.0)
        self.dem_res_spin.setDecimals(1)
        self.dem_res_spin.setValue(0.0)
        self.dem_res_spin.setSpecialValueText("Auto")
        self.dem_res_spin.setSuffix(" cm/px")
        opt.addRow("DEM resolution:", self.dem_res_spin)
        self.dsm_check = QCheckBox("DSM")
        self.dsm_check.setChecked(True)
        self.dtm_check = QCheckBox("DTM / DEM")
        self.dtm_check.setChecked(True)
        self.mesh_check = QCheckBox("3D mesh")
        self.mesh_check.setChecked(True)
        prow = QHBoxLayout()
        prow.addWidget(self.dsm_check)
        prow.addWidget(self.dtm_check)
        prow.addWidget(self.mesh_check)
        prow.addStretch()
        opt.addRow("Products:", prow)
        self.fast_check = QCheckBox("Fast orthophoto (quick, flat scenes)")
        opt.addRow("", self.fast_check)
        self.gcp_widget = QgsFileWidget()
        self.gcp_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.gcp_widget.setFilter("GCP list (*.txt);;All files (*)")
        self.gcp_widget.setDialogTitle("Optional ODM gcp_list.txt")
        opt.addRow("GCP list (optional):", self.gcp_widget)
        self.load_check = QCheckBox("Load results (ortho/DSM/DTM/point cloud) into the QGIS canvas")
        self.load_check.setChecked(True)
        opt.addRow("", self.load_check)
        layout.addWidget(opt_box)

        # close scroll
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ---- log / progress ----
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(130)
        outer.addWidget(self.log_view)
        self.step_label = QLabel("")
        self.step_label.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.step_label)
        pr = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setMinimumWidth(70)
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pr.addWidget(self.progress_bar)
        pr.addWidget(self.elapsed_label)
        outer.addLayout(pr)

        br = QHBoxLayout()
        self.run_button = QPushButton("Process on node")
        self.run_button.clicked.connect(self._on_run_clicked)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.open_folder_button = QPushButton("Open Output Folder")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._on_open_folder_clicked)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        br.addWidget(self.run_button)
        br.addWidget(self.cancel_button)
        br.addWidget(self.open_folder_button)
        br.addStretch()
        br.addWidget(self.close_button)
        outer.addLayout(br)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

    # ---------------------------------------------------------- settings
    def _restore_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        self.url_edit.setText(s.value("url", "http://localhost:3000", type=str) or "http://localhost:3000")
        self.token_edit.setText(s.value("token", "", type=str))
        inp = s.value("input", "", type=str)
        outp = s.value("output", "", type=str)
        s.endGroup()
        if inp and os.path.isdir(inp):
            self.input_widget.setFilePath(inp)
        if outp and os.path.isdir(outp):
            self.output_widget.setFilePath(outp)

    def _save_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("url", self.url_edit.text().strip())
        s.setValue("token", self.token_edit.text().strip())
        s.setValue("input", self.input_widget.filePath().strip())
        s.setValue("output", self.output_widget.filePath().strip())
        s.endGroup()

    # ------------------------------------------------------ live feedback
    def _update_photo_count(self, *_a):
        d = self.input_widget.filePath().strip()
        if not d or not os.path.isdir(d):
            self.photo_count_label.setText("")
            return
        try:
            n = len(find_images(d))
        except Exception:
            self.photo_count_label.setText("")
            return
        if n:
            self.photo_count_label.setText("%d photo(s) found." % n)
            self.photo_count_label.setStyleSheet("color: palette(mid);")
        else:
            self.photo_count_label.setText("No photos found in this folder.")
            self.photo_count_label.setStyleSheet("color: #b03030;")

    def _log(self, msg):
        self.log_view.appendPlainText(msg)
        m = _STEP_RE.match(msg.strip())
        if m:
            self.step_label.setText("Step %s/%s -- %s" % m.groups())

    def _update_elapsed(self):
        if self.start_time is None:
            return
        secs = int(time.time() - self.start_time)
        h, rem = divmod(secs, 3600)
        mm, ss = divmod(rem, 60)
        self.elapsed_label.setText("%d:%02d:%02d" % (h, mm, ss) if h else "%d:%02d" % (mm, ss))

    # ------------------------------------------------------------ actions
    def _client(self):
        if not requests_available():
            QMessageBox.critical(
                self, "Missing dependency",
                "The Python package 'requests' is not installed in QGIS's Python.\n\n"
                "Install it once via the OSGeo4W Shell:\n"
                "    python-qgis -m pip install requests")
            return None
        return NodeODMClient(self.url_edit.text().strip(), token=(self.token_edit.text().strip() or None))

    def _on_test_connection(self):
        c = self._client()
        if c is None:
            return
        self._log("[INFO] Testing node at %s ..." % self.url_edit.text().strip())
        self.test_btn.setEnabled(False)
        try:
            ok, msg = c.test_connection()
        finally:
            self.test_btn.setEnabled(True)
        self._log("[INFO] " + msg)
        if ok:
            QMessageBox.information(self, "Node OK", msg)
        else:
            QMessageBox.warning(self, "Connection failed", msg)

    def _on_run_clicked(self):
        if self._client() is None:
            return
        input_dir = self.input_widget.filePath().strip()
        output_dir = self.output_widget.filePath().strip()
        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "Missing input", "Please select a valid drone photo folder.")
            return
        if not output_dir:
            QMessageBox.warning(self, "Missing output", "Please choose an output folder for the results.")
            return
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "Invalid output folder", str(e))
            return
        if len(find_images(input_dir)) < 2:
            QMessageBox.warning(self, "Not enough photos", "The node needs several overlapping photos (typically 20+).")
            return
        gcp = self.gcp_widget.filePath().strip() or None
        if gcp and not os.path.isfile(gcp):
            QMessageBox.warning(self, "GCP not found", "The GCP file does not exist.")
            return

        self._save_settings()
        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.step_label.setText("")
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_folder_button.setEnabled(False)
        self.last_output_dir = output_dir
        self.start_time = time.time()
        self.elapsed_label.setText("0:00")
        self._elapsed_timer.start()

        self.task = NodeODMTask(
            self.url_edit.text().strip(), input_dir, output_dir,
            token=(self.token_edit.text().strip() or None),
            produce_dsm=self.dsm_check.isChecked(), produce_dtm=self.dtm_check.isChecked(),
            produce_mesh=self.mesh_check.isChecked(), pc_quality=self.pc_combo.currentData(),
            ortho_resolution_cm=self.ortho_res_spin.value(),
            dem_resolution_cm=(self.dem_res_spin.value() or None),
            fast_orthophoto=self.fast_check.isChecked(), gcp_path=gcp,
        )
        self.task.logMessage.connect(self._log)
        self.task.progressChanged.connect(lambda p: self.progress_bar.setValue(int(p)))
        self.task.finishedOk.connect(self._on_finished_ok)
        self.task.finishedError.connect(self._on_finished_error)
        QgsApplication.taskManager().addTask(self.task)

    def _on_cancel_clicked(self):
        if self.task:
            self.task.cancel()
        self.cancel_button.setEnabled(False)
        self._log("\nCanceling... (asking the node to stop the task)")

    def _load_raster(self, path, name):
        if not path or not os.path.isfile(path):
            return
        layer = QgsRasterLayer(path, name)
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            self._log("[WARNING] Could not load into canvas: %s" % path)

    def _load_point_cloud(self, path, name):
        if not path or not os.path.isfile(path):
            return
        try:
            from qgis.core import QgsPointCloudLayer
            layer = QgsPointCloudLayer(path, name, "pdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                return
        except Exception:
            logging.getLogger(__name__).debug("point cloud layer load failed", exc_info=True)
        self._log("[INFO] Point cloud saved (open manually if not shown): %s" % path)

    def _on_finished_ok(self, outputs):
        self._elapsed_timer.stop()
        self._log("\nDone!")
        self.step_label.setText("")
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.open_folder_button.setEnabled(True)
        if self.load_check.isChecked():
            self._load_raster(outputs.get("orthophoto"), "Orthophoto")
            self._load_raster(outputs.get("dsm"), "DSM")
            self._load_raster(outputs.get("dtm"), "DTM")
            self._load_point_cloud(outputs.get("point_cloud"), "Point cloud")
        self.iface.messageBar().pushSuccess("SkyStitch - 3D", "Finished. Results saved to %s" % self.last_output_dir)

    def _on_finished_error(self, message):
        self._elapsed_timer.stop()
        self._log("\n[FAILED] %s" % message)
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        QMessageBox.critical(self, "Processing failed", message)

    def _on_open_folder_clicked(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_output_dir))
