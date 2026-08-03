"""
webodm_worker.py
================
Runs a WebODM job (upload -> process -> download) in a background QgsTask so
QGIS stays responsive. Mirrors worker.py / odm_worker.py: same signal names
and cancellation model.
"""

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .pipeline.webodm_client import process_folder, WebODMError, WebODMCanceled


class WebODMTask(QgsTask):

    logMessage = pyqtSignal(str)
    finishedOk = pyqtSignal(object)   # emits the outputs dict from process_folder()
    finishedError = pyqtSignal(str)

    def __init__(
        self, base_url, username, password, input_dir, output_dir,
        produce_dsm=True, produce_dtm=True, produce_mesh=True,
        pc_quality="medium", ortho_resolution_cm=5.0, dem_resolution_cm=None,
        fast_orthophoto=False, gcp_path=None, project_name="SkyStitch",
        download=("orthophoto", "dsm", "dtm", "point_cloud"),
    ):
        super().__init__("Processing on WebODM", QgsTask.Flag.CanCancel)
        self.base_url = base_url
        self.username = username
        self.password = password
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.produce_dsm = produce_dsm
        self.produce_dtm = produce_dtm
        self.produce_mesh = produce_mesh
        self.pc_quality = pc_quality
        self.ortho_resolution_cm = ortho_resolution_cm
        self.dem_resolution_cm = dem_resolution_cm
        self.fast_orthophoto = fast_orthophoto
        self.gcp_path = gcp_path
        self.project_name = project_name
        self.download = download

        self.outputs = None
        self.error_message = None

    def run(self):
        try:
            self.outputs = process_folder(
                self.base_url, self.username, self.password,
                self.input_dir, self.output_dir,
                produce_dsm=self.produce_dsm, produce_dtm=self.produce_dtm,
                produce_mesh=self.produce_mesh, pc_quality=self.pc_quality,
                ortho_resolution_cm=self.ortho_resolution_cm,
                dem_resolution_cm=self.dem_resolution_cm,
                fast_orthophoto=self.fast_orthophoto, gcp_path=self.gcp_path,
                project_name=self.project_name, download=self.download,
                feedback=lambda m: self.logMessage.emit(m),
                progress=lambda p: self.setProgress(p),
                is_canceled=lambda: self.isCanceled(),
            )
            return True
        except WebODMCanceled:
            self.error_message = "Canceled by user."
            return False
        except WebODMError as e:
            self.error_message = str(e)
            return False
        except Exception as e:  # noqa: BLE001
            self.error_message = "Unexpected error: %s" % e
            return False

    def finished(self, result):
        if result and self.outputs:
            self.finishedOk.emit(self.outputs)
        else:
            self.finishedError.emit(self.error_message or "Process canceled.")
