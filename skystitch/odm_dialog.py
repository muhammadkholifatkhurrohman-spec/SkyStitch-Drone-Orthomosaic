"""
odm_dialog.py
=============
Dialog for SkyStitch's OpenDroneMap (3D) engine: pick a photo folder and a
project/output folder, choose which products to build (DSM, DTM, point
cloud, mesh) and which QGIS derivatives to generate afterwards (contour,
slope, hillshade), then run ODM in the background and load the results.

UI conventions (QgsFileWidget forms, background QgsTask + signals, log view
with a stage label, "load into canvas" behaviour) intentionally follow
``skystitch_dialog.py`` so the two engines feel like one plugin.
"""

import os
import re
import time

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QLabel, QComboBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton, QPlainTextEdit,
    QProgressBar, QMessageBox, QGroupBox, QLineEdit, QScrollArea, QWidget,
    QFrame,
)

from .odm_worker import OdmTask
from .pipeline.odm_engine import find_photos
from .pipeline import derivatives

SETTINGS_GROUP = "SkyStitchODM"
PATTERN_PRESETS = ["*.jpg;*.JPG;*.jpeg;*.JPEG", "*.jpg", "*.JPG", "*.png;*.PNG", "*.tif;*.tiff"]
PC_QUALITY = [
    ("Medium (balanced, default)", "medium"),
    ("High (denser, slower)", "high"),
    ("Ultra (densest, slowest)", "ultra"),
    ("Low (faster)", "low"),
    ("Lowest (fastest, roughest)", "lowest"),
]

_STAGE_RE = re.compile(r"^\[STAGE (\d+)/(\d+)\]\s*(.+)$")


class OdmDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.task = None
        self.start_time = None
        self.last_project_path = None

        self.setWindowTitle("SkyStitch - 3D via local Docker (advanced)")
        self.setMinimumWidth(640)
        # Keep a modest minimum height and a sensible default size; the tall
        # form is inside a scroll area (see _build_ui), so the window never has
        # to grow past the screen.
        self.setMinimumHeight(460)
        self.resize(680, 720)
        self._build_ui()
        self._restore_settings()
        self._update_photo_count()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = QVBoxLayout(self)

        # The form (info + all the option group boxes) can get tall, so it goes
        # inside a vertical scroll area. The log, progress bar and action
        # buttons are added to `outer` further down, so they stay pinned at the
        # bottom of the window and never scroll out of reach.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)  # leave room for the scrollbar

        info = QLabel(
            "Full photogrammetry via OpenDroneMap: builds a true orthophoto plus "
            "DSM, DTM/DEM, point cloud and 3D mesh, and can derive contour/slope/"
            "hillshade in QGIS. Requires Docker (see README -> 'OpenDroneMap (3D) mode')."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # ---- input / output ----
        io_box = QGroupBox("Input / Output")
        io = QFormLayout(io_box)
        io.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.input_widget = QgsFileWidget()
        self.input_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.input_widget.setDialogTitle("Select the folder containing the drone photos")
        self.input_widget.fileChanged.connect(self._update_photo_count)
        io.addRow("Drone photo folder:", self.input_widget)

        self.pattern_combo = QComboBox()
        self.pattern_combo.setEditable(True)
        self.pattern_combo.addItems(PATTERN_PRESETS)
        self.pattern_combo.editTextChanged.connect(self._update_photo_count)
        io.addRow("File name pattern:", self.pattern_combo)

        self.photo_count_label = QLabel("")
        self.photo_count_label.setStyleSheet("color: palette(mid);")
        io.addRow("", self.photo_count_label)

        self.project_widget = QgsFileWidget()
        self.project_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.project_widget.setDialogTitle("Select a working/output folder for OpenDroneMap")
        self.project_widget.setToolTip(
            "ODM writes the whole project here (needs free disk space).\n"
            "Products land in <this folder>/skystitch_odm/ ."
        )
        io.addRow("Output (project) folder:", self.project_widget)
        layout.addWidget(io_box)

        # ---- processing options ----
        opt_box = QGroupBox("Processing options")
        opt = QFormLayout(opt_box)
        opt.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.pc_combo = QComboBox()
        for label, data in PC_QUALITY:
            self.pc_combo.addItem(label, data)
        self.pc_combo.setToolTip("Point cloud density. Higher = more detail but slower and more RAM.")
        opt.addRow("Point cloud quality:", self.pc_combo)

        self.ortho_res_spin = QDoubleSpinBox()
        self.ortho_res_spin.setRange(0.1, 100.0)
        self.ortho_res_spin.setDecimals(1)
        self.ortho_res_spin.setValue(5.0)
        self.ortho_res_spin.setSuffix(" cm/px")
        self.ortho_res_spin.setToolTip("Orthophoto resolution (smaller = sharper, larger files).")
        opt.addRow("Orthophoto resolution:", self.ortho_res_spin)

        self.dem_res_spin = QDoubleSpinBox()
        self.dem_res_spin.setRange(0.0, 100.0)
        self.dem_res_spin.setDecimals(1)
        self.dem_res_spin.setValue(0.0)
        self.dem_res_spin.setSpecialValueText("Auto")
        self.dem_res_spin.setSuffix(" cm/px")
        self.dem_res_spin.setToolTip("DSM/DTM resolution. 'Auto' lets ODM choose from the data.")
        opt.addRow("DEM resolution:", self.dem_res_spin)

        self.dsm_check = QCheckBox("DSM (surface)")
        self.dsm_check.setChecked(True)
        self.dtm_check = QCheckBox("DTM / DEM (ground)")
        self.dtm_check.setChecked(True)
        self.mesh_check = QCheckBox("3D textured mesh")
        self.mesh_check.setChecked(True)
        prod_row = QHBoxLayout()
        prod_row.addWidget(self.dsm_check)
        prod_row.addWidget(self.dtm_check)
        prod_row.addWidget(self.mesh_check)
        prod_row.addStretch()
        opt.addRow("Products:", prod_row)

        self.fast_check = QCheckBox("Fast orthophoto (skip dense cloud; quick 2.5D for flat scenes)")
        opt.addRow("", self.fast_check)
        self.gpu_check = QCheckBox("Use GPU image (opendronemap/odm:gpu, needs NVIDIA + nvidia-docker)")
        opt.addRow("", self.gpu_check)

        self.gcp_widget = QgsFileWidget()
        self.gcp_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.gcp_widget.setFilter("GCP list (*.txt);;All files (*)")
        self.gcp_widget.setDialogTitle("Optional ODM gcp_list.txt for survey-grade accuracy")
        self.gcp_widget.setToolTip(
            "Optional OpenDroneMap gcp_list.txt (surveyed ground control points).\n"
            "Greatly improves absolute accuracy. Leave empty to use photo GPS only."
        )
        opt.addRow("GCP list (optional):", self.gcp_widget)
        layout.addWidget(opt_box)

        # ---- QGIS derivatives ----
        der_box = QGroupBox("Derivatives in QGIS (from the DSM, after processing)")
        der = QFormLayout(der_box)
        der.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.contour_check = QCheckBox("Generate contours")
        self.contour_interval = QDoubleSpinBox()
        self.contour_interval.setRange(0.05, 1000.0)
        self.contour_interval.setDecimals(2)
        self.contour_interval.setValue(1.0)
        self.contour_interval.setSuffix(" m")
        self.contour_interval.setEnabled(False)
        self.contour_check.toggled.connect(self.contour_interval.setEnabled)
        c_row = QHBoxLayout()
        c_row.addWidget(self.contour_check)
        c_row.addWidget(QLabel("Interval:"))
        c_row.addWidget(self.contour_interval)
        c_row.addStretch()
        der.addRow("", c_row)

        self.slope_check = QCheckBox("Slope")
        self.hillshade_check = QCheckBox("Hillshade")
        d_row = QHBoxLayout()
        d_row.addWidget(self.slope_check)
        d_row.addWidget(self.hillshade_check)
        d_row.addStretch()
        der.addRow("", d_row)
        layout.addWidget(der_box)

        # ---- advanced ----
        adv_box = QGroupBox("Advanced")
        adv = QFormLayout(adv_box)
        adv.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.docker_edit = QLineEdit("docker")
        self.docker_edit.setToolTip("Docker executable (e.g. 'docker', 'podman', or a full path).")
        adv.addRow("Docker command:", self.docker_edit)
        docker_btn_row = QHBoxLayout()
        self.test_docker_btn = QPushButton("Test Docker")
        self.test_docker_btn.setToolTip("Check whether Docker is installed and running.")
        self.test_docker_btn.clicked.connect(self._on_test_docker)
        self.install_docker_btn = QPushButton("Install Docker")
        self.install_docker_btn.setToolTip("Install Docker Desktop (Windows: via winget; otherwise opens the download page).")
        self.install_docker_btn.clicked.connect(self._on_install_docker)
        docker_btn_row.addWidget(self.test_docker_btn)
        docker_btn_row.addWidget(self.install_docker_btn)
        docker_btn_row.addStretch()
        adv.addRow("", docker_btn_row)
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(0, 256)
        self.concurrency_spin.setValue(0)
        self.concurrency_spin.setSpecialValueText("Auto")
        self.concurrency_spin.setToolTip("Max CPU threads ODM uses. 'Auto' lets ODM decide.")
        adv.addRow("Max concurrency:", self.concurrency_spin)
        self.load_check = QCheckBox("Load results into the QGIS canvas when finished")
        self.load_check.setChecked(True)
        adv.addRow("", self.load_check)
        layout.addWidget(adv_box)

        # done filling the scrollable form; pin it into the window
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ---- log / progress (fixed below the scroll area) ----
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(130)
        outer.addWidget(self.log_view)

        self.step_label = QLabel("")
        self.step_label.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.step_label)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setMinimumWidth(70)
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.elapsed_label)
        outer.addLayout(progress_row)

        # ---- buttons (fixed) ----
        btn_row = QHBoxLayout()
        self.run_button = QPushButton("Process with OpenDroneMap")
        self.run_button.clicked.connect(self._on_run_clicked)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.open_folder_button = QPushButton("Open Output Folder")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._on_open_folder_clicked)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        btn_row.addWidget(self.run_button)
        btn_row.addWidget(self.cancel_button)
        btn_row.addWidget(self.open_folder_button)
        btn_row.addStretch()
        btn_row.addWidget(self.close_button)
        outer.addLayout(btn_row)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

    # ---------------------------------------------------------- settings
    def _restore_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        inp = s.value("last_input_dir", "", type=str)
        proj = s.value("last_project_dir", "", type=str)
        pattern = s.value("pattern", PATTERN_PRESETS[0], type=str)
        pc = s.value("pc_quality", "medium", type=str)
        ortho = s.value("ortho_res", 5.0, type=float)
        dsm = s.value("dsm", True, type=bool)
        dtm = s.value("dtm", True, type=bool)
        mesh = s.value("mesh", True, type=bool)
        docker = s.value("docker", "docker", type=str)
        load = s.value("load", True, type=bool)
        s.endGroup()

        if inp and os.path.isdir(inp):
            self.input_widget.setFilePath(inp)
        if proj and os.path.isdir(proj):
            self.project_widget.setFilePath(proj)
        if pattern:
            self.pattern_combo.setCurrentText(pattern)
        idx = self.pc_combo.findData(pc)
        if idx >= 0:
            self.pc_combo.setCurrentIndex(idx)
        self.ortho_res_spin.setValue(ortho)
        self.dsm_check.setChecked(dsm)
        self.dtm_check.setChecked(dtm)
        self.mesh_check.setChecked(mesh)
        self.docker_edit.setText(docker or "docker")
        self.load_check.setChecked(load)

    def _save_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("last_input_dir", self.input_widget.filePath().strip())
        s.setValue("last_project_dir", self.project_widget.filePath().strip())
        s.setValue("pattern", self.pattern_combo.currentText().strip())
        s.setValue("pc_quality", self.pc_combo.currentData())
        s.setValue("ortho_res", self.ortho_res_spin.value())
        s.setValue("dsm", self.dsm_check.isChecked())
        s.setValue("dtm", self.dtm_check.isChecked())
        s.setValue("mesh", self.mesh_check.isChecked())
        s.setValue("docker", self.docker_edit.text().strip() or "docker")
        s.setValue("load", self.load_check.isChecked())
        s.endGroup()

    # ------------------------------------------------------ live feedback
    def _update_photo_count(self, *_args):
        input_dir = self.input_widget.filePath().strip()
        pattern = self.pattern_combo.currentText().strip() or PATTERN_PRESETS[0]
        if not input_dir or not os.path.isdir(input_dir):
            self.photo_count_label.setText("")
            return
        try:
            found = find_photos(input_dir, pattern)
        except Exception:
            self.photo_count_label.setText("")
            return
        if found:
            self.photo_count_label.setText(f"{len(found)} photo(s) found.")
            self.photo_count_label.setStyleSheet("color: palette(mid);")
        else:
            self.photo_count_label.setText("No photos found with this pattern in this folder.")
            self.photo_count_label.setStyleSheet("color: #b03030;")

    def _log(self, msg):
        self.log_view.appendPlainText(msg)
        stripped = msg.strip()
        m = _STAGE_RE.match(stripped)
        if m:
            num, total, text = m.groups()
            self.step_label.setText(f"Stage {num}/{total} -- {text}")
        if stripped.startswith("[WARNING]"):
            self.iface.messageBar().pushWarning(
                "SkyStitch - OpenDroneMap", stripped[len("[WARNING]"):].strip()
            )

    def _update_elapsed(self):
        if self.start_time is None:
            return
        secs = int(time.time() - self.start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        self.elapsed_label.setText(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")

    # --------------------------------------------------------------- run
    def _on_run_clicked(self):
        input_dir = self.input_widget.filePath().strip()
        project_dir = self.project_widget.filePath().strip()
        pattern = self.pattern_combo.currentText().strip() or PATTERN_PRESETS[0]

        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "Missing input", "Please select a valid drone photo folder first.")
            return
        if not project_dir:
            QMessageBox.warning(self, "Missing output", "Please choose an output (project) folder first.")
            return
        try:
            os.makedirs(project_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "Invalid output folder", f"Could not create/use:\n{project_dir}\n\n{e}")
            return
        if not os.access(project_dir, os.W_OK):
            QMessageBox.critical(self, "Invalid output folder", f"This folder is not writable:\n{project_dir}")
            return

        found = find_photos(input_dir, pattern)
        if len(found) < 2:
            QMessageBox.warning(
                self, "Not enough photos",
                f"Found {len(found)} photo(s). OpenDroneMap needs several overlapping "
                "photos (typically 20+). For 1-2 photos, use the 2D SkyStitch engine.",
            )
            return

        gcp_path = self.gcp_widget.filePath().strip() or None
        if gcp_path and not os.path.isfile(gcp_path):
            QMessageBox.warning(self, "GCP file not found", f"'{gcp_path}' doesn't exist. Clear the field or pick a valid file.")
            return

        self._save_settings()

        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.step_label.setText("")
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_folder_button.setEnabled(False)

        self.start_time = time.time()
        self.elapsed_label.setText("0:00")
        self._elapsed_timer.start()

        dem_res = self.dem_res_spin.value() or None
        self.task = OdmTask(
            input_dir, project_dir, name="skystitch_odm", pattern=pattern,
            produce_dsm=self.dsm_check.isChecked(),
            produce_dtm=self.dtm_check.isChecked(),
            produce_mesh=self.mesh_check.isChecked(),
            pc_quality=self.pc_combo.currentData(),
            ortho_resolution_cm=self.ortho_res_spin.value(),
            dem_resolution_cm=dem_res,
            fast_orthophoto=self.fast_check.isChecked(),
            gcp_path=gcp_path,
            use_gpu=self.gpu_check.isChecked(),
            docker_exe=self.docker_edit.text().strip() or "docker",
            max_concurrency=(self.concurrency_spin.value() or None),
        )
        self.task.logMessage.connect(self._log)
        self.task.progressChanged.connect(lambda pct: self.progress_bar.setValue(int(pct)))
        self.task.finishedOk.connect(self._on_finished_ok)
        self.task.finishedError.connect(self._on_finished_error)
        QgsApplication.taskManager().addTask(self.task)

    def _on_cancel_clicked(self):
        if self.task:
            self.task.cancel()
        self.cancel_button.setEnabled(False)
        self._log("\nCanceling... (stopping the ODM container)")

    # ----------------------------------------------------------- finish
    def _load_raster(self, path, name):
        if not path or not os.path.isfile(path):
            return False
        layer = QgsRasterLayer(path, name)
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return True
        self._log(f"[WARNING] Could not load layer into canvas: {path}")
        return False

    def _load_point_cloud(self, path, name):
        if not path or not os.path.isfile(path):
            return False
        try:
            from qgis.core import QgsPointCloudLayer
        except Exception:
            self._log("[INFO] Point cloud saved (this QGIS build can't display LAZ directly): " + path)
            return False
        try:
            layer = QgsPointCloudLayer(path, name, "pdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                return True
        except Exception as e:
            self._log(f"[INFO] Point cloud saved but not loaded ({e}): {path}")
        return False

    def _run_derivatives(self, dem_path):
        """Generate the requested QGIS derivatives from the DSM (main thread)."""
        base = os.path.dirname(dem_path)
        der_dir = os.path.join(base, "derivatives")
        os.makedirs(der_dir, exist_ok=True)
        want_load = self.load_check.isChecked()

        if self.contour_check.isChecked():
            try:
                out = derivatives.generate_contour(
                    dem_path, os.path.join(der_dir, "contours.gpkg"),
                    interval=self.contour_interval.value(), feedback=self._log)
                if want_load:
                    from qgis.core import QgsVectorLayer
                    vl = QgsVectorLayer(out, "Contours", "ogr")
                    if vl.isValid():
                        QgsProject.instance().addMapLayer(vl)
            except Exception as e:
                self._log(f"[WARNING] Contour generation failed: {e}")

        if self.slope_check.isChecked():
            try:
                out = derivatives.generate_slope(dem_path, os.path.join(der_dir, "slope.tif"), feedback=self._log)
                if want_load:
                    self._load_raster(out, "Slope")
            except Exception as e:
                self._log(f"[WARNING] Slope generation failed: {e}")

        if self.hillshade_check.isChecked():
            try:
                out = derivatives.generate_hillshade(dem_path, os.path.join(der_dir, "hillshade.tif"), feedback=self._log)
                if want_load:
                    self._load_raster(out, "Hillshade")
            except Exception as e:
                self._log(f"[WARNING] Hillshade generation failed: {e}")

    def _on_finished_ok(self, outputs):
        self._elapsed_timer.stop()
        self._log("\nDone!")
        self.step_label.setText("")
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.open_folder_button.setEnabled(True)
        self.last_project_path = outputs.get("project_path")

        if self.load_check.isChecked():
            self._load_raster(outputs.get("ortho"), "Orthophoto")
            self._load_raster(outputs.get("dsm"), "DSM")
            self._load_raster(outputs.get("dtm"), "DTM")
            self._load_point_cloud(outputs.get("point_cloud"), "Point cloud")

        # derivatives from the DSM (fall back to DTM if DSM wasn't produced)
        dem_for_derivatives = outputs.get("dsm") or outputs.get("dtm")
        if dem_for_derivatives and (
            self.contour_check.isChecked() or self.slope_check.isChecked() or self.hillshade_check.isChecked()
        ):
            self._log("\n[INFO] Generating QGIS derivatives...")
            self._run_derivatives(dem_for_derivatives)
        elif not dem_for_derivatives and (
            self.contour_check.isChecked() or self.slope_check.isChecked() or self.hillshade_check.isChecked()
        ):
            self._log("[WARNING] No DSM/DTM was produced, so contour/slope/hillshade were skipped. Enable DSM and re-run.")

        self.iface.messageBar().pushSuccess(
            "SkyStitch - OpenDroneMap", f"Processing finished. Project: {outputs.get('project_path')}"
        )

    def _on_finished_error(self, message):
        self._elapsed_timer.stop()
        self._log(f"\n[FAILED] {message}")
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        QMessageBox.critical(self, "OpenDroneMap processing failed", message)

    def _on_test_docker(self):
        from .pipeline.odm_engine import docker_status
        exe = self.docker_edit.text().strip() or "docker"
        self._log("[INFO] Testing Docker ('%s')..." % exe)
        st = docker_status(exe)
        self._log("[INFO] " + st["message"])
        if st["found"] and st["daemon"]:
            QMessageBox.information(self, "Docker OK", st["message"])
        elif st["found"]:
            QMessageBox.warning(self, "Docker engine not running", st["message"])
        else:
            QMessageBox.critical(
                self, "Docker not found",
                st["message"] + "\n\nClick 'Install Docker' to install it, then restart Windows and QGIS.")

    def _on_install_docker(self):
        from .pipeline.odm_engine import docker_install_hint, start_docker_install
        method, cmd, url = docker_install_hint()
        if method == "winget":
            msg = ("This will run:\n\n    winget install Docker.DockerDesktop\n\n"
                   "Windows will ask for administrator permission (UAC) and download a few hundred MB. "
                   "When it finishes you must RESTART Windows, then restart QGIS before using the 3D "
                   "engine.\n\nContinue?")
        else:
            msg = ("Docker can't be auto-installed on this OS from here; the official install page will "
                   "open in your browser instead.\n\nContinue?")
        if QMessageBox.question(self, "Install Docker", msg,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        action, arg = start_docker_install(feedback=self._log)
        if action == "started":
            QMessageBox.information(
                self, "Installing Docker",
                "Docker installation was launched in a separate window. Approve the UAC prompt and wait "
                "for it to finish, then RESTART Windows and QGIS before using the 3D engine.")
        else:
            QDesktopServices.openUrl(QUrl(arg or url))
            self._log("[INFO] Opened the Docker download page. After installing, restart your PC and QGIS.")

    def _on_open_folder_clicked(self):
        if self.last_project_path and os.path.isdir(self.last_project_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_project_path))
