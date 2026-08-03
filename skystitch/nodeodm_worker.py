"""
nodeodm_worker.py
=================
Runs a NodeODM job (upload -> process -> download -> extract) in a background
QgsTask. Mirrors the other workers' signals and cancellation model.
"""

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .pipeline.nodeodm_client import process_folder, NodeODMError, NodeODMCanceled


class NodeODMTask(QgsTask):

    logMessage = pyqtSignal(str)
    finishedOk = pyqtSignal(object)
    finishedError = pyqtSignal(str)

    def __init__(
        self, base_url, input_dir, output_dir, token=None,
        produce_dsm=True, produce_dtm=True, produce_mesh=True,
        pc_quality="medium", ortho_resolution_cm=5.0, dem_resolution_cm=None,
        fast_orthophoto=False, gcp_path=None, name="SkyStitch",
    ):
        super().__init__("Processing on NodeODM", QgsTask.Flag.CanCancel)
        self.base_url = base_url
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.token = token
        self.produce_dsm = produce_dsm
        self.produce_dtm = produce_dtm
        self.produce_mesh = produce_mesh
        self.pc_quality = pc_quality
        self.ortho_resolution_cm = ortho_resolution_cm
        self.dem_resolution_cm = dem_resolution_cm
        self.fast_orthophoto = fast_orthophoto
        self.gcp_path = gcp_path
        self.name = name
        self.outputs = None
        self.error_message = None

    def run(self):
        try:
            self.outputs = process_folder(
                self.base_url, self.input_dir, self.output_dir, token=self.token,
                produce_dsm=self.produce_dsm, produce_dtm=self.produce_dtm,
                produce_mesh=self.produce_mesh, pc_quality=self.pc_quality,
                ortho_resolution_cm=self.ortho_resolution_cm,
                dem_resolution_cm=self.dem_resolution_cm,
                fast_orthophoto=self.fast_orthophoto, gcp_path=self.gcp_path, name=self.name,
                feedback=lambda m: self.logMessage.emit(m),
                progress=lambda p: self.setProgress(p),
                is_canceled=lambda: self.isCanceled(),
            )
            return True
        except NodeODMCanceled:
            self.error_message = "Canceled by user."
            return False
        except NodeODMError as e:
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
