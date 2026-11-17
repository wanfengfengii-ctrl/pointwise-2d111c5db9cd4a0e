from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from acquisition.archive.store import ArchiveStore
from acquisition.archive.read import CaptureReader
from acquisition.archive.schema import IntegrityError
from acquisition.delivery.batch import CaptureService
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.export import CaptureExporter
from tests.fixtures import make_case, scenario


class TestDelivery(unittest.TestCase):
    def test_reopen_and_export(self):
        case = make_case(count=12)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'archive.sqlite'
            with ArchiveStore(path) as store:
                receipt = CaptureService(store).ingest('original', case.body)
                frozen = CaptureReader(store).read(receipt.capture_id)
            # Real fresh process forbids extraction imports; reads and export consume stored records.
            script = '''
import importlib.abc, json, sys
class ForbidExtraction(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'acquisition.frames.extract':
            raise AssertionError('fixed reads must not import extractor')
sys.meta_path.insert(0, ForbidExtraction())
from acquisition.archive.store import ArchiveStore
from acquisition.archive.read import CaptureReader
from acquisition.delivery.export import CaptureExporter
with ArchiveStore(sys.argv[1]) as store:
    manifest = CaptureReader(store).read(sys.argv[2])
    CaptureExporter(store).export(sys.argv[2], sys.argv[3])
    print(json.dumps(manifest, sort_keys=True))
'''
            process = subprocess.run([sys.executable, '-B', '-c', script, str(path),
                                      receipt.capture_id, str(root / 'first')],
                                     capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout), frozen)
            delivered = json.loads((root / 'first' / 'manifest.json').read_text())
            self.assertEqual([entry['index'] for entry in delivered['files']], list(range(12)))
            self.assertEqual([entry['filename'] for entry in delivered['files']],
                             ['frame-' + str(i).zfill(6) + '.jpg' for i in range(12)])
            for entry, expected in zip(delivered['files'], case.frames):
                self.assertEqual((root / 'first' / entry['filename']).read_bytes(), expected)
            with ArchiveStore(path) as store:
                other = make_case(count=1, overrides={(8, 0x18): ('UI', b'2.25.987654')})
                CaptureService(store).ingest('later', other.body)
                self.assertEqual(CaptureReader(store).read(receipt.capture_id), frozen)
                CaptureExporter(store).export(receipt.capture_id, root / 'second')
            first = {p.name: p.read_bytes() for p in (root / 'first').iterdir()}
            second = {p.name: p.read_bytes() for p in (root / 'second').iterdir()}
            self.assertEqual(first, second)
            scenario('v7', {'frames': 12, 'later_source': 'later'},
                     list(range(12)), [entry['index'] for entry in delivered['files']],
                     [sha256(frame).hexdigest() for frame in case.frames],
                     [sha256((root / 'second' / entry['filename']).read_bytes()).hexdigest()
                      for entry in delivered['files']],
                     ['acquisition/archive/read.py', 'acquisition/delivery/manifest.py',
                      'acquisition/delivery/audit.py', 'acquisition/delivery/export.py'])

    def test_export_rollback_and_collision(self):
        case = make_case(count=3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ArchiveStore(root / 'archive.sqlite') as store:
                receipt = CaptureService(store).ingest('source', case.body)
                CaptureExporter(store).export(receipt.capture_id, root / 'existing')
                preserved = {p.name: p.read_bytes() for p in (root / 'existing').iterdir()}
                for stage in ('frame:0', 'frame:1', 'before_publish'):
                    target = root / ('failed-' + stage.replace(':', '-'))
                    def fail(current):
                        if current == stage:
                            raise OSError('injected export write failure')
                    with self.assertRaisesRegex(OSError, 'injected export'):
                        CaptureExporter(store, fault=fail).export(receipt.capture_id, target)
                    self.assertFalse(target.exists())
                    self.assertFalse(list(root.glob('.' + target.name + '.staging-*')))
                    self.assertEqual({p.name: p.read_bytes() for p in (root / 'existing').iterdir()}, preserved)
                with self.assertRaises(FileExistsError):
                    CaptureExporter(store).export(receipt.capture_id, root / 'existing')
                self.assertEqual({p.name: p.read_bytes() for p in (root / 'existing').iterdir()}, preserved)
                self.assertEqual(len(EvidenceAuditor(store).audit(receipt.capture_id)), 3)

    def test_audit_detects_corruption(self):
        case = make_case()
        modes = ('raw', 'body', 'projection', 'order', 'index', 'attachment', 'manifest')
        for mode in modes:
            with self.subTest(mode=mode), ArchiveStore(':memory:') as store:
                receipt = CaptureService(store).ingest('source', case.body)
                for frame, spans in zip(case.frames, case.spans):
                    self.assertEqual(b''.join(case.body[a:b] for a, b in spans), frame)
                self.assertEqual([r.digest for r in EvidenceAuditor(store).audit(receipt.capture_id)],
                                 [sha256(frame).hexdigest() for frame in case.frames])
                connection = store.connection
                if mode == 'raw':
                    raw = bytearray(case.body)
                    raw[case.spans[0][0][0]] ^= 1
                    connection.execute('UPDATE sources SET body=?', (bytes(raw),))
                elif mode == 'body':
                    connection.execute('UPDATE frames SET body=? WHERE frame_index=0', (b'bad',))
                elif mode == 'index':
                    connection.execute('UPDATE frames SET frame_index=7 WHERE frame_index=0')
                elif mode == 'attachment':
                    other = CaptureService(store).ingest('other', make_case(count=1).body)
                    connection.execute('PRAGMA foreign_keys=OFF')
                    connection.execute('UPDATE captures SET attachment_id=? WHERE capture_id=?',
                                       ('missing-attachment', receipt.capture_id))
                else:
                    manifest = json.loads(connection.execute(
                        'SELECT manifest FROM captures WHERE capture_id=?', (receipt.capture_id,)).fetchone()[0])
                    if mode == 'manifest':
                        del manifest['metadata']['rows']
                    else:
                        pairs = manifest['frames'][0]['spans']
                        if mode == 'projection':
                            pairs[:] = [[a + 1, b + 1] for a, b in pairs]
                            self.assertNotEqual(b''.join(case.body[a:b] for a, b in pairs), case.frames[0])
                        else:
                            pairs[0], pairs[1] = pairs[1], pairs[0]
                        connection.execute('UPDATE frames SET spans=? WHERE capture_id=? AND frame_index=0',
                                           (json.dumps(pairs), receipt.capture_id))
                    connection.execute('UPDATE captures SET manifest=? WHERE capture_id=?',
                                       (json.dumps(manifest), receipt.capture_id))
                with self.assertRaises(IntegrityError):
                    EvidenceAuditor(store).audit(receipt.capture_id)
                with self.assertRaises(IntegrityError):
                    CaptureReader(store).read(receipt.capture_id)
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / 'delivery'
                    with self.assertRaises(IntegrityError):
                        CaptureExporter(store).export(receipt.capture_id, target)
                    self.assertFalse(target.exists())
