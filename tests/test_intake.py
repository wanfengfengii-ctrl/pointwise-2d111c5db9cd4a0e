import unittest
from hashlib import sha256
from acquisition.archive.store import ArchiveStore
from acquisition.archive.read import CaptureReader
from acquisition.archive.schema import IntegrityError
from acquisition.delivery.batch import CaptureService
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.receipt.identity import SourceConflict
from acquisition.receipt.source import CaptureError
from tests.fixtures import make_case, scenario


class TestIntake(unittest.TestCase):
    def test_multiframe_capture(self):
        case = make_case(nested=True)
        with ArchiveStore(':memory:') as store:
            receipt = CaptureService(store).ingest('device-A', bytearray(case.body))
            reader = CaptureReader(store)
            manifest = reader.read(receipt.capture_id)
            self.assertEqual(manifest['metadata']['frame_count'], 2)
            self.assertEqual((manifest['metadata']['rows'], manifest['metadata']['columns']), (8, 32))
            for index, expected in enumerate(case.frames):
                frame = reader.read(receipt.capture_id, index)
                self.assertEqual(frame.body, expected)
                self.assertEqual(frame.digest, sha256(expected).hexdigest())
                self.assertEqual(tuple((span.start, span.end) for span in frame.spans), case.spans[index])
            audits = EvidenceAuditor(store).audit(receipt.capture_id)
            self.assertEqual([result.size for result in audits], list(map(len, case.frames)))
            scenario('v1', {'frames': 2, 'fragments': len(case.chunks), 'nested': True},
                     [sha256(frame).hexdigest() for frame in case.frames],
                     [reader.read(receipt.capture_id, i).digest for i in range(2)],
                     list(map(len, case.frames)), [result.size for result in audits],
                     ['acquisition/dataset/sequences.py', 'acquisition/encapsulation/offsets.py',
                      'acquisition/frames/extract.py', 'acquisition/archive/publish.py',
                      'acquisition/delivery/audit.py'])

    def test_replay_and_identity_conflict(self):
        case = make_case()
        changed = make_case(frames=tuple(reversed(case.frames)))
        with ArchiveStore(':memory:') as store:
            service = CaptureService(store)
            receipt = service.ingest(' A ', case.body)
            baseline = CaptureReader(store).read(receipt.capture_id)
            counts = [store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('sources', 'captures', 'frames', 'receipts')]
            self.assertEqual(service.ingest(' A ', case.body), receipt)
            with self.assertRaises(SourceConflict):
                service.ingest(' A ', changed.body)
            self.assertEqual(CaptureReader(store).read(receipt.capture_id), baseline)
            self.assertEqual(counts, [store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                      for table in ('sources', 'captures', 'frames', 'receipts')])
            for identity in ('A', 'a', ' A'):
                self.assertNotEqual(service.ingest(identity, case.body).capture_id, receipt.capture_id)
            for identity in ('', None, 1):
                with self.assertRaises(CaptureError):
                    service.ingest(identity, case.body)
            store.connection.execute('UPDATE sources SET body=? WHERE attachment_id=?',
                                     (b'corrupt', receipt.attachment_id))
            with self.assertRaises(IntegrityError):
                service.ingest(' A ', case.body)

    def test_metadata_and_sequences(self):
        with ArchiveStore(':memory:') as store:
            service = CaptureService(store)
            for count in (1, 12):
                for nested in (False, True):
                    case = make_case(count=count, nested=nested,
                                     declared=None if count == 1 else b' 12 ')
                    receipt = service.ingest(str((count, nested)), case.body)
                    metadata = CaptureReader(store).read(receipt.capture_id)['metadata']
                    self.assertEqual(metadata['frame_count'], count)
                    self.assertEqual(metadata['sop_instance_uid'], '2.25.123456')

    def test_sequential_batch_and_snapshot(self):
        case = make_case()
        with ArchiveStore(':memory:') as store:
            service = CaptureService(store)
            results = service.ingest_batch([('first', case.body), ('bad', b'broken'),
                                            ('last', case.body)])
            self.assertEqual([r.source_id for r in results], ['first', 'bad', 'last'])
            self.assertEqual(results[1].error_type, 'CaptureError')
            self.assertIsNone(store.load_receipt('bad'))
            reader = CaptureReader(store)
            for result in (results[0], results[2]):
                self.assertEqual(reader.read(result.receipt.capture_id, 0).body, case.frames[0])
            mutable = bytearray(case.body)
            original = bytes(mutable)
            def change_external(stage):
                if stage == 'source':
                    mutable[:] = b'x' * len(mutable)
            store.fault = change_external
            receipt = service.ingest('mutable', mutable)
            self.assertEqual(store.load_source(receipt.attachment_id)['body'], original)
            self.assertEqual(reader.read(receipt.capture_id, 1).body, case.frames[1])
