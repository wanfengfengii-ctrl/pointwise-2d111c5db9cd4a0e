"""Regressions for the fixed research copy: rewrite invariants, replay,
conflict, transaction rollback, corruption detection and export."""
from hashlib import sha256
import json
from pathlib import Path
import struct
import tempfile
import unittest

import derived_check
from acquisition.archive.read import CaptureReader
from acquisition.archive.schema import IntegrityError, NotFound
from acquisition.archive.store import ArchiveStore
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.batch import CaptureService
from acquisition.delivery.export import CaptureExporter
from acquisition.dataset.rewrite import rewrite_file
from tests.fixtures import make_case
from acquisition.receipt.identity import SourceConflict
from acquisition.receipt.source import CaptureError


def meta_entries(body):
    position = 132
    entries = []
    while body[position:position + 2] == b'\x02\0':
        group, tag = struct.unpack_from('<HH', body, position)
        vr = body[position + 4:position + 6].decode()
        if vr in {'OB', 'OW', 'SQ', 'UN', 'UT'}:
            length = struct.unpack_from('<I', body, position + 8)[0]
            start = position + 12
        else:
            length = struct.unpack_from('<H', body, position + 6)[0]
            start = position + 8
        entries.append(((group, tag), body[start:start + length]))
        position = start + length
    return entries, position


class TestRewriteInvariants(unittest.TestCase):
    def test_simultaneous_self_and_padding(self):
        original, _ = derived_check.samples()
        mapping = {'1.2.3': '2.25.1234', '1.2.4': '1.2.4'}
        derived, seen = rewrite_file(original[0]['body'], mapping)
        self.assertEqual(seen, {'1.2.3', '1.2.4'})
        entries, data_start = meta_entries(derived)
        values = dict(entries)
        # SOP Instance 1.2.3 becomes an odd-length value: exactly one NUL.
        self.assertEqual(values[(2, 3)], b'2.25.1234\0')
        self.assertEqual(values[(2, 0x10)], b'1.2.840.10008.1.2.4.50')
        group_length = struct.unpack_from('<I', values[(2, 0)])[0]
        self.assertEqual(group_length, data_start - (132 + 12))
        # Non-target ImplementationClassUID that literally contains an old UID.
        self.assertEqual(values[(2, 0x12)], b'1.2.3\0')
        # The nested 1.2.4 reference maps to itself; still one NUL padding.
        self.assertIn(b'\x08\x00\x55\x11UI\x06\x001.2.4\0', derived)
        # Deleted and rewritten identity elements are gone from every scope.
        self.assertNotIn(b'INNER^NAME', derived)
        self.assertNotIn(b'patient-7', derived)
        self.assertIn(b'AFTER^PIXEL', original[0]['body'])
        self.assertNotIn(b'AFTER^PIXEL', derived)
        # Pixel Data element bytes are untouched, including the APP literal.
        pixel = original[0]['pixel']
        self.assertIn(pixel, derived)
        self.assertIn(b'APP literal 1.2.3 must stay', derived)
        self.assertLess(data_start, len(derived))

    def test_no_chaining(self):
        original, _ = derived_check.samples()
        mapping = {'1.2.3': '1.2.4', '1.2.4': '2.25.9'}
        derived, _ = rewrite_file(original[0]['body'], mapping)
        entries, data_start = meta_entries(derived)
        values = dict(entries)
        # The instance UID 1.2.3 got the new value 1.2.4 in one pass...
        self.assertEqual(values[(2, 3)], b'1.2.4\0')
        head = derived[:data_start]
        self.assertNotIn(b'2.25.9', head)   # a chained pass would have changed it
        # ...while the old 1.2.4 reference became 2.25.9 (even: no NUL).
        self.assertIn(b'\x08\x00\x55\x11UI\x06\x002.25.9', derived)
        self.assertEqual(derived.count(b'2.25.9'), 2)
        # Everything else, including the untouched APP literal, is identical.
        pixel = original[0]['pixel']
        self.assertIn(b'APP literal 1.2.3 must stay', pixel)
        self.assertIn(pixel, derived)
        self.assertNotIn(b'INNER^NAME', derived)

    def test_container_forms_and_coverage(self):
        original, expected = derived_check.samples()
        for index in range(2):
            derived, _ = rewrite_file(original[index]['body'], derived_check.UID_MAP)
            self.assertEqual(derived, expected[index]['body'])
            self.assertEqual(len(derived), len(expected[index]['body']))
            self.assertEqual(derived[:132], original[index]['body'][:132])
            # Delimiters and header layout survive byte for byte.
            self.assertEqual(derived.count(b'\xfe\xff\xdd\xe0'),
                             original[index]['body'].count(b'\xfe\xff\xdd\xe0'))
            self.assertEqual(derived.count(b'\xfe\xff\x0d\xe0'),
                             original[index]['body'].count(b'\xfe\xff\x0d\xe0'))
            self.assertEqual(derived[-len(original[index]['pixel']):],
                             original[index]['pixel'])
        with self.assertRaises(CaptureError):
            rewrite_file(original[0]['body'], {'1.2.3': '2.25.1'})
        with self.assertRaises(CaptureError):
            rewrite_file(original[0]['body'], dict(derived_check.UID_MAP, extra='2.25.2'))
        with self.assertRaises(CaptureError):
            rewrite_file(original[0]['body'], {'1.2.3': '2.25.1', '1.2.4': '2.25.1'})
        # Coverage (no unused key) is enforced at package level, not per file.
        _, seen = rewrite_file(original[0]['body'], {'1.2.3': '2.25.1', '1.2.4': '2.25.2',
                                                     '1.2.9': '2.25.3'})
        self.assertEqual(seen, {'1.2.3', '1.2.4'})
        with self.assertRaises(CaptureError):
            rewrite_file(original[0]['body'], {'1.2.3': '2.025.1', '1.2.4': '2.25.2'})
        with self.assertRaises(CaptureError):
            rewrite_file(original[0]['body'], {'1..2': '2.25.1', '1.2.4': '2.25.2'})


class TestDerivedDelivery(unittest.TestCase):
    def test_replay_conflict_and_caller_isolation(self):
        with ArchiveStore(':memory:') as store:
            original, expected, receipts = derived_check.seed(store)
            ids = [receipt.capture_id for receipt in receipts]
            service = CaptureService(store)
            self.assertEqual(service.derive('pkg', ids, dict(derived_check.UID_MAP)), 'pkg')
            # Dict order is irrelevant; ordered parents are not.
            flipped = dict(reversed(list(derived_check.UID_MAP.items())))
            self.assertEqual(service.derive('pkg', tuple(ids), flipped), 'pkg')
            for changed_ids, changed_map in ((list(reversed(ids)), derived_check.UID_MAP),
                                             (ids, {'1.2.3': '2.25.1', '1.2.4': '2.25.2'}),
                                             (ids, {'1.2.4': '2.25.678901', '1.2.3': '2.25.12345',
                                                    '9.9': '2.25.3'})):
                with self.assertRaises((SourceConflict, CaptureError)):
                    service.derive('pkg', changed_ids, changed_map)
            value = CaptureReader(store).read_derived('pkg')
            derived_check.check_result(value, 'pkg', receipts, original, expected)
            value['files'][0]['body'] = b'changed by caller'
            value['files'].clear()
            again = CaptureReader(store).read_derived('pkg')
            derived_check.check_result(again, 'pkg', receipts, original, expected)
            with self.assertRaises(NotFound):
                CaptureReader(store).read_derived('absent')
            with self.assertRaises(NotFound):
                EvidenceAuditor(store).audit_derived('absent')
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(NotFound):
                    CaptureExporter(store).export_derived('absent', Path(directory) / 'x')

    def test_multi_frame_parents(self):
        with ArchiveStore(':memory:') as store:
            service = CaptureService(store)
            parents = [service.ingest('multi-0', make_case(count=3).body),
                       service.ingest('multi-1', make_case(count=2, nested=True).body)]
            ids = [receipt.capture_id for receipt in parents]
            mapping = {'2.25.123456': '2.25.500001'}
            self.assertEqual(service.derive('multi', ids, mapping), 'multi')
            value = CaptureReader(store).read_derived('multi')
            self.assertEqual([len(entry['frames']) for entry in value['files']], [3, 2])
            self.assertTrue(EvidenceAuditor(store).audit_derived('multi'))
            for entry in value['files']:
                source_body = store.load_source(
                    store.load_capture(entry['parent_capture_id'])['attachment_id'])['body']
                for frame in entry['frames']:
                    self.assertEqual(b''.join(entry['body'][a:b]
                                              for a, b in frame['derived_spans']),
                                     b''.join(source_body[a:b]
                                              for a, b in frame['source_spans']))
                    self.assertEqual(sha256(b''.join(
                        entry['body'][a:b] for a, b in frame['derived_spans'])).hexdigest(),
                        frame['digest'])

    def test_failed_input_consumes_no_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite'
            with ArchiveStore(path) as store:
                original, expected, receipts = derived_check.seed(store)
                ids = [receipt.capture_id for receipt in receipts]
                old = [CaptureReader(store).read(receipt.capture_id) for receipt in receipts]
                bad = derived_check.sample_layout(1, derived_check.OLD[1],
                                                  derived_check.OLD[0], True, 'LO')
                bad_receipt = CaptureService(store).ingest('bad-vr', bad['body'])
                with self.assertRaises(CaptureError):
                    CaptureService(store).derive('retry', [ids[0], bad_receipt.capture_id],
                                                 derived_check.UID_MAP)
                self.assertIsNone(store.load_derived('retry'))
                def fail(stage):
                    if stage == 'before_commit':
                        raise OSError('derive commit interrupted')
                store.fault = fail
                with self.assertRaisesRegex(OSError, 'derive commit interrupted'):
                    CaptureService(store).derive('retry', ids, derived_check.UID_MAP)
            with ArchiveStore(path) as store:
                self.assertIsNone(store.load_derived('retry'))
                self.assertEqual(store.connection.execute(
                    'SELECT count(*) FROM derived_files').fetchone()[0], 0)
                self.assertEqual([CaptureReader(store).read(receipt.capture_id)
                                  for receipt in receipts], old)
                self.assertEqual(CaptureService(store).derive('retry', ids,
                                                              derived_check.UID_MAP), 'retry')
                derived_check.check_result(CaptureReader(store).read_derived('retry'), 'retry',
                                           receipts, original, expected)

    def test_export_shape_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ArchiveStore(root / 'archive.sqlite') as store:
                original, expected, receipts = derived_check.seed(store)
                ids = [receipt.capture_id for receipt in receipts]
                CaptureService(store).derive('research', ids, derived_check.UID_MAP)
                delivery = CaptureExporter(store).export_derived('research', root / 'out')
                self.assertEqual(delivery, json.loads((root / 'out' / 'manifest.json').read_text()))
                self.assertEqual(delivery['capture_ids'], ids)
                self.assertEqual(delivery['uid_map'], derived_check.UID_MAP)
                for index, entry in enumerate(delivery['files']):
                    self.assertNotIn('body', entry)
                    self.assertEqual(entry['parent_capture_id'], ids[index])
                    self.assertEqual(entry['filename'], 'dicom-' + str(index).zfill(6) + '.dcm')
                    self.assertEqual((root / 'out' / entry['filename']).read_bytes(),
                                     expected[index]['body'])
                preserved = {path.name: path.read_bytes() for path in (root / 'out').iterdir()}
                with self.assertRaises(FileExistsError):
                    CaptureExporter(store).export_derived('research', root / 'out')
                self.assertEqual({path.name: path.read_bytes()
                                  for path in (root / 'out').iterdir()}, preserved)
                def fail(stage):
                    if stage == 'before_publish':
                        raise OSError('export interrupted')
                with self.assertRaisesRegex(OSError, 'export interrupted'):
                    CaptureExporter(store, fault=fail).export_derived('research', root / 'failed')
                self.assertFalse((root / 'failed').exists())
                self.assertFalse(list(root.glob('.failed.staging-*')))
                with self.assertRaises(FileNotFoundError):
                    CaptureExporter(store).export_derived('research', root / 'no-parent' / 'out')
                # The pre-existing JPEG delivery API still works unchanged.
                first = receipts[0].capture_id
                legacy = CaptureExporter(store).export(first, root / 'legacy')
                self.assertEqual([entry['filename'] for entry in legacy.value['files']],
                                 ['frame-000000.jpg'])
                self.assertEqual((root / 'legacy' / 'frame-000000.jpg').read_bytes(),
                                 original[0]['frame'])

    def _corrupt(self, mutate):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / 'archive.sqlite'
        with ArchiveStore(path) as store:
            original, expected, receipts = derived_check.seed(store)
            ids = [receipt.capture_id for receipt in receipts]
            CaptureService(store).derive('pkg', ids, derived_check.UID_MAP)
            manifest = json.loads(store.connection.execute(
                "SELECT manifest FROM derived_packages WHERE package_id='pkg'").fetchone()[0])
            bodies = [row[0] for row in store.connection.execute(
                "SELECT body FROM derived_files WHERE package_id='pkg' ORDER BY file_index")]
            mutate(store, manifest, bodies)
            store.connection.execute("UPDATE derived_packages SET manifest=? WHERE package_id='pkg'",
                                     (json.dumps(manifest),))
            for index, body in enumerate(bodies):
                store.connection.execute(
                    'UPDATE derived_files SET body=? WHERE package_id=? AND file_index=?',
                    (body, 'pkg', index))
        with ArchiveStore(path) as store:
            with self.assertRaises(IntegrityError):
                CaptureReader(store).read_derived('pkg')
            with self.assertRaises(IntegrityError):
                EvidenceAuditor(store).audit_derived('pkg')
            with self.assertRaises(IntegrityError):
                CaptureExporter(store).export_derived('pkg', root / 'rejected')
            self.assertFalse((root / 'rejected').exists())
            # A replay must not silently accept or repair the corruption.
            with self.assertRaises(IntegrityError):
                CaptureService(store).derive('pkg', ids, derived_check.UID_MAP)

    def test_corrupt_derived_body_despite_new_digest(self):
        def mutate(store, manifest, bodies):
            raw = bytearray(bodies[0])
            raw[3] ^= 0xff          # inside the 128-byte preamble, outside every frame
            bodies[0] = bytes(raw)
            manifest['files'][0]['derived_digest'] = sha256(bodies[0]).hexdigest()
        self._corrupt(mutate)

    def test_corrupt_mapping(self):
        def mutate(store, manifest, bodies):
            manifest['uid_map'] = {'1.2.3': '1.2.3', '1.2.4': '1.2.4'}
        self._corrupt(mutate)

    def test_corrupt_parent_binding(self):
        def mutate(store, manifest, bodies):
            files = manifest['files']
            first, second = manifest['capture_ids']
            manifest['capture_ids'] = [second, first]
            files[0]['parent_capture_id'] = second
            files[1]['parent_capture_id'] = first
            files[0]['source_digest'], files[1]['source_digest'] = (
                files[1]['source_digest'], files[0]['source_digest'])
        self._corrupt(mutate)

    def test_corrupt_parent_source_digest(self):
        def mutate(store, manifest, bodies):
            manifest['files'][0]['source_digest'] = '0' * 64
        self._corrupt(mutate)

    def test_corrupt_frame_spans(self):
        def mutate(store, manifest, bodies):
            spans = manifest['files'][0]['frames'][0]['derived_spans']
            manifest['files'][0]['frames'][0]['derived_spans'] = [
                [start + 2, end + 2] for start, end in spans]
        self._corrupt(mutate)

    def test_corrupt_source_spans_with_consistent_rewrite(self):
        def mutate(store, manifest, bodies):
            frame = manifest['files'][0]['frames'][0]
            spans = frame['source_spans']
            frame['source_spans'] = [[start + 2, end + 2] for start, end in spans]
        self._corrupt(mutate)


class TestDerivedArchiveShape(unittest.TestCase):
    def test_legacy_archive_gains_derived_tables_additively(self):
        import sqlite3
        from acquisition.archive.store import DDL
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite'
            connection = sqlite3.connect(path)
            for statement in DDL[:5]:
                connection.execute(statement)
            connection.execute('INSERT INTO archive_info VALUES (1)')
            connection.commit()
            connection.close()
            with ArchiveStore(path) as store:
                original, expected, receipts = derived_check.seed(store)
                ids = [receipt.capture_id for receipt in receipts]
                before = store.connection.execute(
                    'SELECT count(*) FROM captures').fetchone()[0]
                self.assertEqual(CaptureService(store).derive('pkg', ids,
                                                              derived_check.UID_MAP), 'pkg')
                self.assertEqual(store.connection.execute(
                    'SELECT count(*) FROM captures').fetchone()[0], before)
                derived_check.check_result(CaptureReader(store).read_derived('pkg'), 'pkg',
                                           receipts, original, expected)
            connection = sqlite3.connect(path)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            connection.close()
            self.assertEqual(tables, {'archive_info', 'sources', 'captures', 'frames',
                                      'receipts', 'derived_packages', 'derived_files'})

    def test_foreign_schema_still_rejected(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite'
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE unrelated (value TEXT)')
            connection.commit()
            connection.close()
            with self.assertRaises(IntegrityError):
                ArchiveStore(path)


if __name__ == '__main__':
    unittest.main()
