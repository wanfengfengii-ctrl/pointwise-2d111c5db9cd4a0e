"""Finite byte fixtures and public joint checks; this is not a DICOM rewriter."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tests.fixtures import element, item, delimiter, jpeg, segment, make_case
from acquisition.archive.store import ArchiveStore
from acquisition.archive.read import CaptureReader
from acquisition.archive.schema import IntegrityError, NotFound
from acquisition.delivery.batch import CaptureService
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.export import CaptureExporter
from acquisition.receipt.source import CaptureError
from acquisition.receipt.identity import SourceConflict

OLD = ('1.2.3', '1.2.4')
UID_MAP = {'1.2.3': '2.25.12345', '1.2.4': '2.25.678901'}


def sample_layout(index, instance, reference, personal, reference_vr='UI'):
    # Assemble one known layout from constants. Never reads or transforms a DICOM.
    frame = jpeg(index)
    frame = frame[:2] + segment(0xe2, b'APP literal 1.2.3 must stay') + frame[2:]
    base = make_case(frames=(frame,), split=(2,), overrides={(8, 0x18): ('UI', instance.encode())})
    syntax = element(2, 0x10, 'UI', b'1.2.840.10008.1.2.4.50')
    old_meta_size = 12 + len(syntax)
    meta_values = (element(2, 2, 'UI', b'1.2.840.10008.5.1.4.1.1.7')
                   + element(2, 3, 'UI', instance.encode()) + syntax
                   + element(2, 0x12, 'UI', b'1.2.3'))
    meta = element(2, 0, 'UL', struct.pack('<I', len(meta_values))) + meta_values
    pn = element(0x10, 0x10, 'PN', b'INNER^NAME') if personal else b''
    pid = element(0x10, 0x20, 'LO', b'patient-7') if personal else b''
    ref = element(8, 0x1155, reference_vr, reference.encode())
    inner = element(8, 0x1140, 'SQ', item(ref + pn))
    undefined = element(8, 0x1111, 'SQ',
                        item(pid + inner, True) + delimiter(0xe00d)
                        + delimiter(0xe0dd), True)
    bounded = element(8, 0x1115, 'SQ', item(pn + ref + undefined))
    opaque = element(0x19, 0x1010, 'UN', b'opaque 1.2.3 \x00\xff')
    prefix = b'R' * 128 + b'DICM' + meta + pn + pid + bounded + opaque
    dataset = base.body[132 + old_meta_size:]
    tail = element(0x10, 0x10, 'PN', b'AFTER^PIXEL') if personal else b''
    body = prefix + dataset + tail
    shift = len(prefix) - (132 + old_meta_size)
    spans = [[a + shift, b + shift] for a, b in base.spans[0]]
    return {'body': body, 'frame': frame, 'spans': spans,
            'pixel': base.body[base.pixel_start - 12:],
            'pixel_start': base.pixel_start - 12 + shift,
            'bounded_length': len(bounded) - 12,
            'meta_length': len(meta_values)}


def samples():
    original = tuple(sample_layout(i, OLD[i], OLD[1-i], True) for i in range(2))
    expected = tuple(sample_layout(i, UID_MAP[OLD[i]], UID_MAP[OLD[1-i]], False)
                     for i in range(2))
    return original, expected


def seed(store):
    original, expected = samples()
    service = CaptureService(store)
    receipts = [service.ingest('sample-' + str(i), value['body'])
                for i, value in enumerate(original)]
    return original, expected, receipts


def check_result(value, package_id, receipts, original, expected):
    assert value['package_id'] == package_id
    assert value['capture_ids'] == [r.capture_id for r in receipts]
    assert value['uid_map'] == UID_MAP
    assert len(value['files']) == len(receipts)
    for entry, receipt, source, result in zip(value['files'], receipts, original, expected):
        assert entry['parent_capture_id'] == receipt.capture_id
        assert entry['source_digest'] == sha256(source['body']).hexdigest()
        assert entry['body'] == result['body']
        assert entry['derived_digest'] == sha256(result['body']).hexdigest()
        assert len(entry['frames']) == 1
        frame = entry['frames'][0]
        assert frame['index'] == 0 and frame['size'] == len(source['frame'])
        assert frame['digest'] == sha256(source['frame']).hexdigest()
        assert frame['source_spans'] == source['spans']
        assert frame['derived_spans'] == result['spans']
        assert b''.join(source['body'][a:b] for a, b in frame['source_spans']) == source['frame']
        assert b''.join(entry['body'][a:b] for a, b in frame['derived_spans']) == source['frame']


def fixture_check():
    original, expected = samples()
    summaries = []
    with ArchiveStore(':memory:') as store:
        for i, (left, right) in enumerate(zip(original, expected)):
            for side, sample in (('original', left), ('expected', right)):
                receipt = CaptureService(store).ingest(side + str(i), sample['body'])
                frame = CaptureReader(store).read(receipt.capture_id, 0)
                assert frame.body == sample['frame']
                assert [[s.start, s.end] for s in frame.spans] == sample['spans']
                assert len(EvidenceAuditor(store).audit(receipt.capture_id)) == 1
            assert left['pixel'] == right['pixel']
            assert left['frame'] == right['frame']
            assert left['spans'] != right['spans']
            assert b''.join(right['body'][a:b] for a, b in left['spans']) != left['frame']
            summaries.append({'file': i, 'meta_lengths': [left['meta_length'], right['meta_length']],
                              'sq_lengths': [left['bounded_length'], right['bounded_length']],
                              'pixel_starts': [left['pixel_start'], right['pixel_start']],
                              'jpeg_sha256': sha256(left['frame']).hexdigest()})
        naive = original[0]['body'].replace(b'1.2.3', UID_MAP['1.2.3'].encode())
        assert b'APP literal 2.25.12345 must stay' in naive
        try:
            CaptureService(store).ingest('naive', naive)
        except CaptureError:
            pass
        else:
            raise AssertionError('raw replacement unexpectedly accepted')
        # V1 accepts this nested LO; V2 must reject the target's wrong VR.
        bad = sample_layout(1, OLD[1], OLD[0], True, 'LO')
        CaptureService(store).ingest('bad-v2-target', bad['body'])
    print(json.dumps({'fixture_check': 'ok', 'files': summaries,
                      'raw_replace_rejected': True, 'v1_accepts_nested_lo': True},
                     sort_keys=True))


class DerivedChecks(unittest.TestCase):
    def test_fixed_reopen_and_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'archive.sqlite'
            with ArchiveStore(path) as store:
                original, expected, receipts = seed(store)
                old = [CaptureReader(store).read(r.capture_id) for r in receipts]
                service = CaptureService(store)
                ids = [r.capture_id for r in receipts]
                self.assertEqual(service.derive('research', ids, dict(UID_MAP)), 'research')
                reader = CaptureReader(store)
                check_result(reader.read_derived('research'), 'research',
                             receipts, original, expected)
                self.assertEqual(service.derive('research', ids,
                                 dict(reversed(list(UID_MAP.items())))), 'research')
                for changed_ids, changed_map in ((list(reversed(ids)), UID_MAP),
                        (ids, {'1.2.3': '2.25.9', '1.2.4': '2.25.8'})):
                    with self.assertRaises(SourceConflict):
                        service.derive('research', changed_ids, changed_map)
            with ArchiveStore(path) as store:
                value = CaptureReader(store).read_derived('research')
                check_result(value, 'research', receipts, original, expected)
                self.assertIs(EvidenceAuditor(store).audit_derived('research'), True)
                self.assertEqual([CaptureReader(store).read(r.capture_id) for r in receipts], old)
                target = root / 'delivery'
                manifest = CaptureExporter(store).export_derived('research', target)
                self.assertEqual(json.loads((target / 'manifest.json').read_text()), manifest)
                self.assertEqual(manifest['package_id'], 'research')
                self.assertEqual(manifest['uid_map'], UID_MAP)
                self.assertEqual(manifest['capture_ids'], ids)
                self.assertEqual(len(manifest['files']), 2)
                for i, entry in enumerate(manifest['files']):
                    self.assertNotIn('body', entry)
                    self.assertEqual(entry['filename'], 'dicom-' + str(i).zfill(6) + '.dcm')
                    self.assertEqual((target / entry['filename']).read_bytes(), expected[i]['body'])
                with self.assertRaises(FileExistsError):
                    CaptureExporter(store).export_derived('research', target)
                def fail(stage):
                    if stage == 'before_publish':
                        raise OSError('delivery interrupted')
                with self.assertRaisesRegex(OSError, 'delivery interrupted'):
                    CaptureExporter(store, fault=fail).export_derived('research', root / 'failed')
                self.assertFalse((root / 'failed').exists())
                self.assertFalse(list(root.glob('.failed.staging-*')))

    def test_invalid_inputs_and_late_bad_target(self):
        with ArchiveStore(':memory:') as store:
            original, expected, receipts = seed(store)
            ids = [r.capture_id for r in receipts]
            service = CaptureService(store)
            reader = CaptureReader(store)
            invalid = [([], UID_MAP), ([ids[0], ids[0]], UID_MAP),
                       (ids + ['absent'], UID_MAP), (ids, {}),
                       (ids, dict(UID_MAP, **{'1.2.9': '2.25.7'})),
                       (ids, {'1.2.3': '2.25.7', '1.2.4': '2.25.7'}),
                       (ids, {'1.2.3': '2.025.7', '1.2.4': '2.25.8'}),
                       (ids, {'1.2.3': '1' * 65, '1.2.4': '2.25.8'})]
            for index, (parents, mapping) in enumerate(invalid):
                package = 'invalid-' + str(index)
                with self.assertRaises(CaptureError):
                    service.derive(package, parents, mapping)
                with self.assertRaises(NotFound):
                    reader.read_derived(package)
            bad = sample_layout(1, OLD[1], OLD[0], True, 'LO')
            bad_receipt = service.ingest('nested-wrong-vr', bad['body'])
            with self.assertRaises(CaptureError):
                service.derive('retryable', [ids[0], bad_receipt.capture_id], UID_MAP)
            with self.assertRaises(NotFound):
                reader.read_derived('retryable')
            self.assertEqual(service.derive('retryable', ids, UID_MAP), 'retryable')
            check_result(reader.read_derived('retryable'), 'retryable',
                         receipts, original, expected)

    def test_transaction_failure_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite'
            with ArchiveStore(path) as store:
                original, expected, receipts = seed(store)
                ids = [r.capture_id for r in receipts]
                old = [CaptureReader(store).read(r.capture_id) for r in receipts]
                def fail(stage):
                    if stage == 'before_commit':
                        raise OSError('derive commit interrupted')
                store.fault = fail
                with self.assertRaisesRegex(OSError, 'derive commit interrupted'):
                    CaptureService(store).derive('retry', ids, UID_MAP)
            with ArchiveStore(path) as store:
                with self.assertRaises(NotFound):
                    CaptureReader(store).read_derived('retry')
                self.assertEqual([CaptureReader(store).read(r.capture_id) for r in receipts], old)
                self.assertEqual(CaptureService(store).derive('retry', ids, UID_MAP), 'retry')
                check_result(CaptureReader(store).read_derived('retry'), 'retry',
                             receipts, original, expected)

    def test_audit_uses_archived_parent(self):
        with ArchiveStore(':memory:') as store:
            original, expected, receipts = seed(store)
            CaptureService(store).derive('audit', [r.capture_id for r in receipts], UID_MAP)
            self.assertIs(EvidenceAuditor(store).audit_derived('audit'), True)
            raw = bytearray(original[0]['body'])
            raw[-1] ^= 1
            store.connection.execute('UPDATE sources SET body=? WHERE attachment_id=?',
                                     (bytes(raw), receipts[0].attachment_id))
            with self.assertRaises(IntegrityError):
                EvidenceAuditor(store).audit_derived('audit')
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'rejected'
                with self.assertRaises(IntegrityError):
                    CaptureExporter(store).export_derived('audit', target)
                self.assertFalse(target.exists())


def demo(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'archive.sqlite'
    with ArchiveStore(path) as store:
        original, expected, receipts = seed(store)
        CaptureService(store).derive('research', [r.capture_id for r in receipts], UID_MAP)
    with ArchiveStore(path) as store:
        value = CaptureReader(store).read_derived('research')
        check_result(value, 'research', receipts, original, expected)
        assert EvidenceAuditor(store).audit_derived('research') is True
        target = root / 'delivery'
        if not target.exists():
            CaptureExporter(store).export_derived('research', target)
        manifest = json.loads((target / 'manifest.json').read_text())
        assert manifest['uid_map'] == UID_MAP and len(manifest['files']) == 2
        for entry, sample in zip(manifest['files'], expected):
            assert (target / entry['filename']).read_bytes() == sample['body']
        print(json.dumps({'package_id': 'research', 'uid_map': UID_MAP,
                          'reopened_audit': True,
                          'files': [{'parent': entry['parent_capture_id'],
                                     'derived_sha256': entry['derived_digest'],
                                     'source_spans': entry['frames'][0]['source_spans'],
                                     'derived_spans': entry['frames'][0]['derived_spans']}
                                    for entry in value['files']]}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['fixtures', 'verify', 'demo'])
    parser.add_argument('--directory', default='derived-demo')
    args = parser.parse_args()
    if args.mode == 'fixtures':
        fixture_check()
        return 0
    if args.mode == 'demo':
        demo(args.directory)
        return 0
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DerivedChecks)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
