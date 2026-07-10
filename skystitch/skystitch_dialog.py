"""
skystitch_dialog.py
======================
Main dialog: input form (photo folder, output file, advanced options)
+ log area & progress bar while the process runs.
"""

import os
import time

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QMessageBox,
    QGroupBox,
)

from .worker import SkyStitchTask
from .pipeline.core import find_photos

SETTINGS_GROUP = "SkyStitch"
PATTERN_PRESETS = [
    "*.jpg",
    "*.jpg;*.jpeg",
    "*.JPG",
    "*.jpg;*.JPG;*.jpeg;*.JPEG",
    "*.png",
]


class SkyStitchDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.task = None
        self.start_time = None
        self.last_output_path = None

        self.setWindowTitle("SkyStitch - Drone Orthomosaic")
        self.setMinimumWidth(600)
        self._build_ui()
        self._restore_settings()
        self._update_photo_count()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "Build an orthomosaic from raw drone photos (JPG with GPS EXIF).\n"
            "Best suited for relatively flat terrain. Photos must overlap by at least 60-70%."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form_box = QGroupBox("Input / Output")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignRight)

        self.input_widget = QgsFileWidget()
        self.input_widget.setStorageMode(QgsFileWidget.GetDirectory)
        self.input_widget.setDialogTitle("Select the folder containing the drone photos")
        self.input_widget.fileChanged.connect(self._update_photo_count)
        form.addRow("Drone photo folder:", self.input_widget)

        self.pattern_combo = QComboBox()
        self.pattern_combo.setEditable(True)
        self.pattern_combo.addItems(PATTERN_PRESETS)
        self.pattern_combo.setToolTip(
            "Which photo file names to include. Use ';' to combine several\n"
            "patterns, e.g. '*.jpg;*.jpeg' to match both extensions."
        )
        self.pattern_combo.editTextChanged.connect(self._update_photo_count)
        form.addRow("File name pattern:", self.pattern_combo)

        self.photo_count_label = QLabel("")
        self.photo_count_label.setStyleSheet("color: palette(mid);")
        form.addRow("", self.photo_count_label)

        self.output_widget = QgsFileWidget()
        self.output_widget.setStorageMode(QgsFileWidget.SaveFile)
        self.output_widget.setFilter("GeoTIFF (*.tif *.tiff)")
        self.output_widget.setDialogTitle("Save orthomosaic result as")
        form.addRow("Output file (.tif):", self.output_widget)

        layout.addWidget(form_box)

        adv_box = QGroupBox("Advanced options")
        adv_form = QFormLayout(adv_box)
        adv_form.setLabelAlignment(Qt.AlignRight)

        self.max_photos_spin = QSpinBox()
        self.max_photos_spin.setRange(0, 100000)
        self.max_photos_spin.setValue(0)
        self.max_photos_spin.setSpecialValueText("All photos")
        self.max_photos_spin.setToolTip(
            "Limit the number of photos processed, useful for a quick test run\n"
            "before running the full process on hundreds of photos."
        )
        self.max_photos_spin.valueChanged.connect(self._update_photo_count)
        adv_form.addRow("Limit photo count:", self.max_photos_spin)

        self.gsd_check = QCheckBox("Set manually")
        self.gsd_spin = QDoubleSpinBox()
        self.gsd_spin.setDecimals(4)
        self.gsd_spin.setRange(0.0001, 100.0)
        self.gsd_spin.setValue(0.03)
        self.gsd_spin.setSuffix(" m/pixel")
        self.gsd_spin.setEnabled(False)
        self.gsd_spin.setToolTip(
            "Ground Sample Distance: real-world size of one output pixel.\n"
            "Left unset, SkyStitch estimates it automatically from photo altitude/overlap."
        )
        self.gsd_check.toggled.connect(self.gsd_spin.setEnabled)
        gsd_row = QHBoxLayout()
        gsd_row.addWidget(self.gsd_check)
        gsd_row.addWidget(self.gsd_spin)
        gsd_row.addStretch()
        adv_form.addRow("Ground Sample Distance:", gsd_row)

        self.load_check = QCheckBox("Load result into the QGIS canvas when finished")
        self.load_check.setChecked(True)
        adv_form.addRow("", self.load_check)

        layout.addWidget(adv_box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)
        layout.addWidget(self.log_view)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setMinimumWidth(70)
        self.elapsed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.elapsed_label)
        layout.addLayout(progress_row)

        btn_row = QHBoxLayout()
        self.run_button = QPushButton("Build Mosaic")
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
        layout.addLayout(btn_row)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

    # ---------------------------------------------------------- settings
    def _restore_settings(self):
        """Remember the last-used folder/pattern/options between sessions."""
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        last_input = s.value("last_input_dir", "", type=str)
        last_pattern = s.value("last_pattern", "*.jpg", type=str)
        last_load = s.value("load_into_canvas", True, type=bool)
        s.endGroup()

        if last_input and os.path.isdir(last_input):
            self.input_widget.setFilePath(last_input)
        if last_pattern:
            self.pattern_combo.setCurrentText(last_pattern)
        self.load_check.setChecked(last_load)

    def _save_settings(self, input_dir, pattern):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("last_input_dir", input_dir)
        s.setValue("last_pattern", pattern)
        s.setValue("load_into_canvas", self.load_check.isChecked())
        s.endGroup()

    # ------------------------------------------------------ live feedback
    def _update_photo_count(self, *_args):
        """Show how many photos currently match the folder+pattern, so
        mistakes (wrong folder, wrong pattern) are caught before running
        the (potentially long) build instead of after it fails."""
        input_dir = self.input_widget.filePath().strip()
        pattern = self.pattern_combo.currentText().strip() or "*.jpg"

        if not input_dir or not os.path.isdir(input_dir):
            self.photo_count_label.setText("")
            return

        try:
            found = find_photos(input_dir, pattern)
        except Exception:
            self.photo_count_label.setText("")
            return

        limit = self.max_photos_spin.value()
        if limit and limit < len(found):
            self.photo_count_label.setText(
                f"{len(found)} photo(s) found -- only the first {limit} will be used."
            )
            self.photo_count_label.setStyleSheet("color: palette(mid);")
        elif found:
            self.photo_count_label.setText(f"{len(found)} photo(s) found.")
            self.photo_count_label.setStyleSheet("color: palette(mid);")
        else:
            self.photo_count_label.setText("No photos found with this pattern in this folder.")
            self.photo_count_label.setStyleSheet("color: #b03030;")

    # --------------------------------------------------------------- logic
    def _log(self, msg):
        self.log_view.appendPlainText(msg)

    def _on_run_clicked(self):
        input_dir = self.input_widget.filePath().strip()
        output_path = self.output_widget.filePath().strip()
        pattern = self.pattern_combo.currentText().strip() or "*.jpg"

        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "Missing input", "Please select a valid drone photo folder first.")
            return
        if not output_path:
            QMessageBox.warning(self, "Missing input", "Please set the output file (.tif) location first.")
            return
        if not output_path.lower().endswith((".tif", ".tiff")):
            output_path += ".tif"

        out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(
                    self, "Invalid output location", f"Could not create the output folder:\n{out_dir}\n\n{e}"
                )
                return
        if not os.access(out_dir, os.W_OK):
            QMessageBox.critical(
                self, "Invalid output location", f"This folder is not writable:\n{out_dir}"
            )
            return

        found = find_photos(input_dir, pattern)
        if len(found) < 2:
            QMessageBox.warning(
                self,
                "Not enough photos",
                f"Only {len(found)} photo(s) matched pattern '{pattern}' in this folder.\n"
                "At least 2 overlapping photos are required.",
            )
            return

        if os.path.exists(output_path):
            reply = QMessageBox.question(
                self,
                "Overwrite file?",
                f"'{os.path.basename(output_path)}' already exists. Overwrite it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        max_photos = self.max_photos_spin.value() or None
        gsd = self.gsd_spin.value() if self.gsd_check.isChecked() else None

        self._save_settings(input_dir, pattern)

        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_folder_button.setEnabled(False)
        self.last_output_path = output_path

        self.start_time = time.time()
        self.elapsed_label.setText("0:00")
        self._elapsed_timer.start()

        self.task = SkyStitchTask(input_dir, output_path, pattern, max_photos, gsd)
        self.task.logMessage.connect(self._log)
        self.task.progressChanged.connect(lambda pct: self.progress_bar.setValue(int(pct)))
        self.task.finishedOk.connect(self._on_finished_ok)
        self.task.finishedError.connect(self._on_finished_error)
        QgsApplication.taskManager().addTask(self.task)

    def _update_elapsed(self):
        if self.start_time is None:
            return
        secs = int(time.time() - self.start_time)
        m, s = divmod(secs, 60)
        self.elapsed_label.setText(f"{m}:{s:02d}")

    def _on_cancel_clicked(self):
        if self.task:
            self.task.cancel()
        self.cancel_button.setEnabled(False)
        self._log("\nCanceling... (finishing the current step first)")

    def _on_finished_ok(self, output_path):
        self._elapsed_timer.stop()
        self._log("\nDone!")
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.open_folder_button.setEnabled(True)

        if self.load_check.isChecked():
            layer_name = os.path.splitext(os.path.basename(output_path))[0]
            layer = QgsRasterLayer(output_path, layer_name)
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self.iface.messageBar().pushSuccess(
                    "SkyStitch - Drone Orthomosaic", f"Orthomosaic built and loaded successfully: {output_path}"
                )
            else:
                self.iface.messageBar().pushWarning(
                    "SkyStitch - Drone Orthomosaic", f"Orthomosaic saved to {output_path}, but failed to load into the canvas."
                )
        else:
            self.iface.messageBar().pushSuccess(
                "SkyStitch - Drone Orthomosaic", f"Orthomosaic built successfully: {output_path}"
            )

    def _on_finished_error(self, message):
        self._elapsed_timer.stop()
        self._log(f"\n[FAILED] {message}")
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        QMessageBox.critical(self, "Failed to build orthomosaic", message)

    def _on_open_folder_clicked(self):
        if self.last_output_path:
            folder = os.path.dirname(os.path.abspath(self.last_output_path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
