import os
from pathlib import Path
import shutil
import tempfile
from acquisition.archive.read import CaptureReader
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.manifest import build_delivery_manifest, encode_delivery_manifest


class CaptureExporter:
    def __init__(self, store, fault=None):
        self.store = store
        self.fault = fault

    def export(self, capture_id, destination):
        target = Path(destination)
        if os.path.lexists(target):
            raise FileExistsError(str(target))
        manifest, frames = CaptureReader(self.store).checked_capture(capture_id)
        EvidenceAuditor(self.store).audit(capture_id)
        delivery = build_delivery_manifest(manifest)
        staging = Path(tempfile.mkdtemp(prefix='.' + target.name + '.staging-', dir=target.parent))
        try:
            for entry, frame in zip(delivery.value['files'], frames):
                path = staging / entry['filename']
                with path.open('xb') as handle:
                    handle.write(frame.body)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.fault is not None:
                    self.fault('frame:' + str(frame.index))
            with (staging / 'manifest.json').open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(encode_delivery_manifest(delivery))
                handle.flush()
                os.fsync(handle.fileno())
            if self.fault is not None:
                self.fault('before_publish')
            if os.path.lexists(target):
                raise FileExistsError(str(target))
            os.rename(staging, target)
            return delivery
        finally:
            if staging.exists():
                shutil.rmtree(staging)
