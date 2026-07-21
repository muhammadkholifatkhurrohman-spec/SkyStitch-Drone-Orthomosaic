"""
worker.py
=========
Runs the orthomosaic pipeline in a background thread (QgsTask) so the
QGIS UI doesn't freeze while it runs (can take tens of minutes to hours
for hundreds of photos). Progress & log text are sent to the dialog via
Qt signals.
"""

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .pipeline.core import run_pipeline, PipelineError, PipelineCanceled


class SkyStitchTask(QgsTask):

    logMessage = pyqtSignal(str)
    finishedOk = pyqtSignal(str, str, float, str)   # output path, preview path, final GSD (m/px), CRS
    finishedError = pyqtSignal(str)  # emits error message

    def __init__(
        self, input_dir, output_path, pattern, max_photos, gsd,
        exposure_compensation=True, gcp_path=None,
        compression="deflate", jpeg_quality=85,
    ):
        super().__init__("Building orthomosaic", QgsTask.Flag.CanCancel)
        self.input_dir = input_dir
        self.output_path = output_path
        self.pattern = pattern
        self.max_photos = max_photos
        self.gsd = gsd
        self.exposure_compensation = exposure_compensation
        self.gcp_path = gcp_path
        self.compression = compression
        self.jpeg_quality = jpeg_quality
        self.error_message = None
        self.result_path = None
        self.preview_path = None
        self.final_gsd = None
        self.final_crs = None

    def run(self):
        """Runs in a separate thread. MUST NOT touch Qt widgets directly here."""
        try:
            self.result_path, self.preview_path, self.final_gsd, self.final_crs = run_pipeline(
                self.input_dir,
                self.output_path,
                pattern=self.pattern,
                max_photos=self.max_photos,
                gsd=self.gsd,
                exposure_compensation=self.exposure_compensation,
                gcp_path=self.gcp_path,
                compression=self.compression,
                jpeg_quality=self.jpeg_quality,
                feedback=lambda msg: self.logMessage.emit(msg),
                progress=lambda pct: self.setProgress(pct),
                is_canceled=lambda: self.isCanceled(),
            )
            return True
        except PipelineCanceled:
            self.error_message = "Canceled by user."
            return False
        except PipelineError as e:
            self.error_message = str(e)
            return False
        except Exception as e:  # noqa: BLE001 - still catch unexpected errors and show them to the user
            self.error_message = f"Unexpected error: {e}"
            return False

    def finished(self, result):
        """Called back on the main thread after run() completes."""
        if result and self.result_path:
            self.finishedOk.emit(self.result_path, self.preview_path or "", self.final_gsd or 0.0, self.final_crs or "")
        else:
            self.finishedError.emit(self.error_message or "Process canceled.")
