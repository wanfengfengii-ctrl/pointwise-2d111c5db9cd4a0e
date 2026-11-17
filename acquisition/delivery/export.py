import json
import os
from pathlib import Path
import shutil
import tempfile

from acquisition.archive.read import CaptureReader
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.manifest import build_delivery_manifest, encode_delivery_manifest


def derived_filename(index):
    if type(index) is not int or index < 0:
        raise ValueError('file index must be nonnegative')
    return 'dicom-' + str(index).zfill(6) + '.dcm'


def build_derived_manifest(value):
    files = []
    for index, entry in enumerate(value['files']):
        files.append({'parent_capture_id': entry['parent_capture_id'],
                      'source_digest': entry['source_digest'],
                      'derived_digest': entry['derived_digest'],
                      'filename': derived_filename(index),
                      'frames': entry['frames']})
    return {'package_id': value['package_id'], 'capture_ids': list(value['capture_ids']),
            'uid_map': dict(value['uid_map']), 'files': files}


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

    def export_derived(self, package_id, destination):
        target = Path(destination)
        if os.path.lexists(target):
            raise FileExistsError(str(target))
        # Complete the account first: archived parents and copies are re-parsed
        # and the fixed rewrite reproven before anything is staged.
        from acquisition.archive.derived import load_verified_package
        value, bodies = load_verified_package(self.store, package_id, deep=True)
        delivery = build_derived_manifest(value)
        staging = Path(tempfile.mkdtemp(prefix='.' + target.name + '.staging-', dir=target.parent))
        try:
            for entry, body in zip(delivery['files'], bodies):
                path = staging / entry['filename']
                with path.open('xb') as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.fault is not None:
                    self.fault('file:' + str(entry['filename']))
            with (staging / 'manifest.json').open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(delivery, ensure_ascii=True, sort_keys=True,
                                        separators=(',', ':')) + '\n')
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
