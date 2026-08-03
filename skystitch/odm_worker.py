"""
odm_worker.py
=============
Runs the OpenDroneMap engine (``pipeline/odm_engine.py``) in a background
QgsTask so the QGIS UI stays responsive during what can be a long (tens of
minutes to hours) photogrammetry run. Mirrors ``worker.py`` -- same signal
names, same cancellation model -- so the dialog wires it up the same way.

Only the ODM run happens here. Derivative products (contour/slope/etc.) are
generated afterwards on the main thread by the dialog, because QGIS
Processing algorithms are happiest run from the main thread.
"""

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .pipeline.odm_engine import run_odm, OdmError, OdmCanceled


class OdmTask(QgsTask):

    logMessage = pyqtSignal(str)
    finishedOk = pyqtSignal(object)   # emits the outputs dict from run_odm()
    finishedError = pyqtSignal(str)   # emits an error message

    def __init__(
        self, input_dir, project_dir, name="skystitch_odm",
        pattern="*.jpg;*.JPG;*.jpeg;*.JPEG",
        produce_dsm=True, produce_dtm=True, produce_mesh=True,
        pc_quality="medium", ortho_resolution_cm=5.0, dem_resolution_cm=None,
        fast_orthophoto=False, gcp_path=None, use_gpu=False,
        docker_exe="docker", max_concurrency=None,
    ):
        super().__init__("Processing with OpenDroneMap", QgsTask.Flag.CanCancel)
        self.input_dir = input_dir
        self.project_dir = project_dir
        self.name = name
        self.pattern = pattern
        self.produce_dsm = produce_dsm
        self.produce_dtm = produce_dtm
        self.produce_mesh = produce_mesh
        self.pc_quality = pc_quality
        self.ortho_resolution_cm = ortho_resolution_cm
        self.dem_resolution_cm = dem_resolution_cm
        self.fast_orthophoto = fast_orthophoto
        self.gcp_path = gcp_path
        self.use_gpu = use_gpu
        self.docker_exe = docker_exe
        self.max_concurrency = max_concurrency

        self.outputs = None
        self.error_message = None

    def run(self):
        """Runs in a worker thread. MUST NOT touch Qt widgets directly."""
        try:
            self.outputs = run_odm(
                self.input_dir,
                self.project_dir,
                name=self.name,
                pattern=self.pattern,
                produce_dsm=self.produce_dsm,
                produce_dtm=self.produce_dtm,
                produce_mesh=self.produce_mesh,
                pc_quality=self.pc_quality,
                ortho_resolution_cm=self.ortho_resolution_cm,
                dem_resolution_cm=self.dem_resolution_cm,
                fast_orthophoto=self.fast_orthophoto,
                gcp_path=self.gcp_path,
                use_gpu=self.use_gpu,
                docker_exe=self.docker_exe,
                max_concurrency=self.max_concurrency,
                feedback=lambda msg: self.logMessage.emit(msg),
                progress=lambda pct: self.setProgress(pct),
                is_canceled=lambda: self.isCanceled(),
            )
            return True
        except OdmCanceled:
            self.error_message = "Canceled by user."
            return False
        except OdmError as e:
            self.error_message = str(e)
            return False
        except Exception as e:  # noqa: BLE001 - surface unexpected errors to the user
            self.error_message = f"Unexpected error: {e}"
            return False

    def finished(self, result):
        """Called back on the main thread after run() completes."""
        if result and self.outputs:
            self.finishedOk.emit(self.outputs)
        else:
            self.finishedError.emit(self.error_message or "Process canceled.")
