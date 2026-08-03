"""
skystitch_dialog.py
======================
Main dialog: input form (photo folder, output file, advanced options)
+ log area & progress bar while the process runs.
"""

import logging
import os
import time

import re

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

_log = logging.getLogger(__name__)

# Matches the "[STEP x/6] ..." lines emitted by pipeline/core.py, so the
# dialog can show which stage is currently running next to the progress
# bar instead of making the user scroll the log to find out.
_STEP_RE = re.compile(r"^\[STEP (\d+)/(\d+)\]\s*(.+)$")

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
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.input_widget = QgsFileWidget()
        self.input_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
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

        self.effort_label = QLabel("")
        self.effort_label.setStyleSheet("color: palette(mid); font-style: italic;")
        self.effort_label.setWordWrap(True)
        form.addRow("", self.effort_label)

        self.output_widget = QgsFileWidget()
        self.output_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.output_widget.setFilter("GeoTIFF (*.tif *.tiff);;JPEG2000 (*.jp2)")
        self.output_widget.setDialogTitle("Save orthomosaic result as")
        form.addRow("Output file:", self.output_widget)

        layout.addWidget(form_box)

        adv_box = QGroupBox("Advanced options")
        adv_form = QFormLayout(adv_box)
        adv_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

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

        self.exposure_check = QCheckBox("Compensate brightness differences between photos")
        self.exposure_check.setChecked(True)
        self.exposure_check.setToolTip(
            "Equalizes each photo's overall brightness to the group's median before\n"
            "blending, so a frame taken under a passing cloud (or slightly different\n"
            "auto-exposure) doesn't show up as a visible brightness seam. Turn off if\n"
            "you'd rather keep each photo's original exposure untouched."
        )
        adv_form.addRow("", self.exposure_check)

        self.compression_combo = QComboBox()
        self.compression_combo.addItem("DEFLATE (lossless, default)", "deflate")
        self.compression_combo.addItem("ZSTD (lossless, smaller/faster than DEFLATE if available)", "zstd")
        self.compression_combo.addItem("LZW (lossless)", "lzw")
        self.compression_combo.addItem("JPEG2000 (lossy, usually the smallest files, keeps transparency)", "jp2")
        self.compression_combo.addItem("JPEG (lossy, small files)", "jpeg")
        self.compression_combo.addItem("None (uncompressed, largest files)", "none")
        self.compression_combo.setToolTip(
            "Compression used when writing the output raster.\n"
            "DEFLATE/ZSTD/LZW are lossless (pixel-perfect, larger files).\n"
            "JPEG2000 is lossy but usually produces the smallest files of all, with a real\n"
            "transparency band even though it's lossy -- saved as .jp2 instead of .tif.\n"
            "Requires this GDAL install to support JPEG2000 (usually does); falls back to\n"
            "DEFLATE automatically if not.\n"
            "JPEG is also lossy (small files, small quality loss, edges of the transparent\n"
            "mask are stored separately since JPEG-in-TIFF has no alpha band).\n"
            "None keeps every pixel exact but produces the largest files by far."
        )
        self.jpeg_quality_spin = QSpinBox()
        self.jpeg_quality_spin.setRange(1, 100)
        self.jpeg_quality_spin.setValue(85)
        self.jpeg_quality_spin.setSuffix(" %")
        self.jpeg_quality_spin.setToolTip("Only used with JPEG or JPEG2000 compression. Higher = better quality & bigger file.")
        self.jpeg_quality_spin.setEnabled(False)
        self.compression_combo.currentIndexChanged.connect(
            lambda _i: self.jpeg_quality_spin.setEnabled(self.compression_combo.currentData() in ("jpeg", "jp2"))
        )
        compression_row = QHBoxLayout()
        compression_row.addWidget(self.compression_combo)
        compression_row.addWidget(QLabel("Quality:"))
        compression_row.addWidget(self.jpeg_quality_spin)
        compression_row.addStretch()
        adv_form.addRow("Output compression:", compression_row)

        self.gcp_widget = QgsFileWidget()
        self.gcp_widget.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self.gcp_widget.setFilter("GCP file (*.csv *.xlsx);;CSV (*.csv);;Excel (*.xlsx)")
        self.gcp_widget.setDialogTitle("Select an optional GCP/ICP correction file (CSV or Excel)")
        self.gcp_widget.setToolTip(
            "Optional: a CSV or Excel (.xlsx) file with surveyed, high-accuracy\n"
            "coordinates for specific photos (columns: photo/filename, x/easting/lon,\n"
            "y/northing/lat). Matched photos are used as trusted anchors, refined with\n"
            "an iterative (ICP-style) reweighting pass, to correct the mosaic's\n"
            "position/scale/rotation beyond plain consumer-GPS accuracy.\n"
            "Leave empty to use GPS only."
        )
        adv_form.addRow("GCP / ICP file (optional):", self.gcp_widget)

        layout.addWidget(adv_box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)
        layout.addWidget(self.log_view)

        self.step_label = QLabel("")
        self.step_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.step_label)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setMinimumWidth(70)
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.elapsed_label)
        layout.addLayout(progress_row)

        # Final CRS + GSD, shown as a proper (selectable/copyable) label
        # once a build finishes -- previously this only appeared buried in
        # the log text, even though it's often needed elsewhere (e.g. to
        # set the CRS of a different layer to match, or to report survey
        # accuracy).
        self.result_info_label = QLabel("")
        self.result_info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_info_label.setVisible(False)
        layout.addWidget(self.result_info_label)

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
        last_exposure = s.value("exposure_compensation", True, type=bool)
        last_compression = s.value("compression", "deflate", type=str)
        last_jpeg_quality = s.value("jpeg_quality", 85, type=int)
        s.endGroup()

        if last_input and os.path.isdir(last_input):
            self.input_widget.setFilePath(last_input)
        if last_pattern:
            self.pattern_combo.setCurrentText(last_pattern)
        self.load_check.setChecked(last_load)
        self.exposure_check.setChecked(last_exposure)
        idx = self.compression_combo.findData(last_compression)
        if idx >= 0:
            self.compression_combo.setCurrentIndex(idx)
        self.jpeg_quality_spin.setValue(last_jpeg_quality)

    def _save_settings(self, input_dir, pattern):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("last_input_dir", input_dir)
        s.setValue("last_pattern", pattern)
        s.setValue("load_into_canvas", self.load_check.isChecked())
        s.setValue("exposure_compensation", self.exposure_check.isChecked())
        s.setValue("compression", self.compression_combo.currentData())
        s.setValue("jpeg_quality", self.jpeg_quality_spin.value())
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
            self.effort_label.setText("")
            return

        try:
            found = find_photos(input_dir, pattern)
        except Exception:
            self.photo_count_label.setText("")
            self.effort_label.setText("")
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

        self._update_effort_estimate(found, limit)

    def _update_effort_estimate(self, found, limit):
        """Rough, cheap-to-compute heads-up on runtime and peak RAM, shown
        BEFORE the user clicks 'Build Mosaic' -- so a folder with hundreds
        of large photos doesn't turn into a multi-hour surprise. This is
        deliberately approximate (actual time depends heavily on overlap,
        CPU core count, and disk speed); it's meant as a ballpark, not a
        promise."""
        paths = found[:limit] if limit else found
        n = len(paths)
        if n < 2:
            self.effort_label.setText("")
            return

        # Peek just the header of a handful of photos (fast -- doesn't
        # decode full pixel data) to get a representative resolution.
        sample = paths[:: max(1, len(paths) // 5)][:5]
        megapixels = []
        try:
            from PIL import Image
            for p in sample:
                try:
                    with Image.open(p) as im:
                        w, h = im.size
                    megapixels.append((w * h) / 1_000_000.0)
                except Exception:
                    # Best-effort only: this is just a rough pre-run time/
                    # RAM estimate sampled from a handful of photos, not
                    # the actual pipeline run -- one unreadable/corrupt
                    # sample photo shouldn't block the estimate, the rest
                    # of the sample is still useful. Logged at debug level
                    # so a systematic problem (e.g. every photo failing)
                    # is still discoverable instead of just showing a
                    # blank estimate with no explanation.
                    _log.debug("Could not read size of sample photo '%s' for effort estimate", p, exc_info=True)
                    continue
        except ImportError:
            pass

        if not megapixels:
            self.effort_label.setText("")
            return
        avg_mp = sum(megapixels) / len(megapixels)

        cpu_count = max(1, os.cpu_count() or 1)

        # Feature detection (SIFT) parallelizes across CPU cores.
        detect_secs = (n * avg_mp * 0.6) / min(cpu_count, n)
        # Pairwise matching among geographically overlapping neighbors.
        match_secs = n * avg_mp * 0.05
        # Warp & blend onto the shared canvas -- the canvas grows with n,
        # but (with ~60-70% overlap) each new photo mostly adds new area
        # rather than fully overlapping the existing mosaic.
        canvas_mp = avg_mp * (1 + (n - 1) * 0.35)
        render_secs = canvas_mp * 0.15
        fixed_overhead_secs = 10

        total_secs = detect_secs + match_secs + render_secs + fixed_overhead_secs
        low_secs, high_secs = total_secs * 0.6, total_secs * 1.8

        # Peak RAM is dominated by the blending accumulation buffers
        # (float32 color + weight arrays sized to the full canvas).
        peak_mb = (canvas_mp * 1_000_000 * 16) / (1024 * 1024) + 300

        self.effort_label.setText(
            f"Estimated: ~{self._format_minutes(low_secs)}-{self._format_minutes(high_secs)}, "
            f"~{peak_mb:,.0f} MB RAM at peak (rough estimate, varies with overlap & hardware)."
        )

    @staticmethod
    def _format_minutes(secs):
        secs = max(0, secs)
        if secs < 60:
            return f"{int(secs)}s"
        mins = secs / 60.0
        if mins < 60:
            return f"{mins:.0f} min"
        h = int(mins // 60)
        m = int(mins % 60)
        return f"{h}h{m:02d}m"

    # --------------------------------------------------------------- logic
    def _log(self, msg):
        self.log_view.appendPlainText(msg)
        stripped = msg.strip()

        m = _STEP_RE.match(stripped)
        if m:
            step_num, step_total, step_text = m.groups()
            self.step_label.setText(f"Step {step_num}/{step_total} -- {step_text}")

        # Warnings (unrecognized camera, GPS baseline too tight, terrain
        # relief, ...) matter enough that they shouldn't only be visible by
        # scrolling the log -- surface them in the QGIS message bar too.
        if stripped.startswith("[WARNING]"):
            self.iface.messageBar().pushWarning(
                "SkyStitch - Drone Orthomosaic", stripped[len("[WARNING]"):].strip()
            )

    def _on_run_clicked(self):
        input_dir = self.input_widget.filePath().strip()
        output_path = self.output_widget.filePath().strip()
        pattern = self.pattern_combo.currentText().strip() or "*.jpg"

        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "Missing input", "Please select a valid drone photo folder first.")
            return
        if not output_path:
            QMessageBox.warning(self, "Missing input", "Please set the output file location first.")
            return
        compression = self.compression_combo.currentData()
        if compression == "jp2":
            if not output_path.lower().endswith(".jp2"):
                output_path = os.path.splitext(output_path)[0] + ".jp2"
        elif not output_path.lower().endswith((".tif", ".tiff")):
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
        if len(found) < 1:
            QMessageBox.warning(
                self,
                "No photos found",
                f"No photo(s) matched pattern '{pattern}' in this folder.",
            )
            return
        if len(found) == 1:
            QMessageBox.information(
                self,
                "Single photo",
                "Only 1 photo matched -- there's nothing to stitch it against, so it will be "
                "georeferenced directly from its own GPS position and camera parameters "
                "instead of built into a multi-photo mosaic.",
            )

        if os.path.exists(output_path):
            reply = QMessageBox.question(
                self,
                "Overwrite file?",
                f"'{os.path.basename(output_path)}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        max_photos = self.max_photos_spin.value() or None
        gsd = self.gsd_spin.value() if self.gsd_check.isChecked() else None

        gcp_path = self.gcp_widget.filePath().strip() or None
        if gcp_path and not os.path.isfile(gcp_path):
            QMessageBox.warning(self, "GCP file not found", f"'{gcp_path}' doesn't exist. Clear the field or pick a valid file.")
            return

        self._save_settings(input_dir, pattern)

        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.step_label.setText("")
        self.result_info_label.setVisible(False)
        self.result_info_label.setText("")
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_folder_button.setEnabled(False)
        self.last_output_path = output_path

        self.start_time = time.time()
        self.elapsed_label.setText("0:00")
        self._elapsed_timer.start()

        self.task = SkyStitchTask(
            input_dir, output_path, pattern, max_photos, gsd,
            exposure_compensation=self.exposure_check.isChecked(),
            gcp_path=gcp_path,
            compression=self.compression_combo.currentData(),
            jpeg_quality=self.jpeg_quality_spin.value(),
        )
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

    def _on_finished_ok(self, output_path, _preview_path, final_gsd, final_crs):
        # _preview_path (the downsized preview .jpg the pipeline saves next
        # to the .tif, see pipeline/core.py::_save_preview) is intentionally
        # not shown in the dialog anymore -- the UI preview thumbnail was
        # removed from the finishing step.
        self._elapsed_timer.stop()
        self._log("\nDone!")
        self.step_label.setText("")
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.open_folder_button.setEnabled(True)

        info_parts = []
        if final_gsd:
            info_parts.append(f"Resolution: {final_gsd:.4f} m/px")
        if final_crs:
            info_parts.append(f"CRS: {final_crs}")
        if info_parts:
            self.result_info_label.setText("  |  ".join(info_parts))
            self.result_info_label.setVisible(True)

        gsd_suffix = f" (final resolution: {final_gsd:.4f} m/px)" if final_gsd else ""

        if self.load_check.isChecked():
            layer_name = os.path.splitext(os.path.basename(output_path))[0]
            layer = QgsRasterLayer(output_path, layer_name)
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self.iface.messageBar().pushSuccess(
                    "SkyStitch - Drone Orthomosaic",
                    f"Orthomosaic built and loaded successfully: {output_path}{gsd_suffix}",
                )
            else:
                self.iface.messageBar().pushWarning(
                    "SkyStitch - Drone Orthomosaic", f"Orthomosaic saved to {output_path}, but failed to load into the canvas."
                )
        else:
            self.iface.messageBar().pushSuccess(
                "SkyStitch - Drone Orthomosaic", f"Orthomosaic built successfully: {output_path}{gsd_suffix}"
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
