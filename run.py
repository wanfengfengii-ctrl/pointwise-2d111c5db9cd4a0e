import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from acquisition.archive.store import ArchiveStore
from acquisition.archive.schema import NotFound
from acquisition.archive.read import CaptureReader
from acquisition.delivery.batch import CaptureService
from acquisition.delivery.audit import EvidenceAuditor
from acquisition.delivery.export import CaptureExporter
from acquisition.delivery.manifest import build_delivery_manifest
from acquisition.receipt.source import CaptureError


def verify():
    root = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), top_level_dir=str(root))
    import derived_check
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(derived_check.DerivedChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def demo(directory):
    from hashlib import sha256
    from tests.fixtures import make_case, scenario
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    settings = json.loads((Path(__file__).resolve().parent / 'samples' / 'normal_case.json').read_text())
    case = make_case(count=settings['frame_count'], table=settings['basic_offset_table'],
                     nested=settings['nested_sequences'])
    path = root / 'archive.sqlite'
    with ArchiveStore(path) as store:
        service = CaptureService(store)
        receipt = service.ingest(settings['source_id'], case.body)
        store.initialize()
        assert service.ingest(settings['source_id'], case.body) == receipt
        original = CaptureReader(store).read(receipt.capture_id)
    with ArchiveStore(path) as store:
        CaptureService(store).ingest('later-export', make_case(count=1).body)
        reader = CaptureReader(store)
        assert reader.read(receipt.capture_id) == original
        audits = EvidenceAuditor(store).audit(receipt.capture_id)
        expected = [sha256(frame).hexdigest() for frame in case.frames]
        actual = [reader.read(receipt.capture_id, i).digest for i in range(len(case.frames))]
        assert actual == expected
        target = root / 'research-delivery'
        if not target.exists():
            CaptureExporter(store).export(receipt.capture_id, target)
        delivered = json.loads((target / 'manifest.json').read_text())
        assert delivered == build_delivery_manifest(original).value
        downstream = []
        for entry, frame in zip(delivered['files'], case.frames):
            body = (target / entry['filename']).read_bytes()
            assert body == frame
            downstream.append(sha256(body).hexdigest())
        assert downstream == expected
        scenario('v7', settings, expected, actual, expected, downstream,
                 ['acquisition/receipt/intake.py', 'acquisition/delivery/batch.py',
                  'acquisition/archive/publish.py', 'acquisition/archive/read.py',
                  'acquisition/delivery/audit.py', 'acquisition/delivery/export.py'])
        # Derived research copy along the same run manifest: two parents whose
        # instance UIDs and cross-references change length, an APP literal that
        # must not, then a close/reopen delivery.
        import derived_check
        research = root / 'derived-research'
        derived_original, derived_expected = derived_check.samples()
        parents = []
        for index, sample in enumerate(derived_original):
            parents.append(CaptureService(store).ingest('research-' + str(index), sample['body']))
        package = CaptureService(store).derive(
            'research', [receipt.capture_id for receipt in parents], derived_check.UID_MAP)
        assert package == 'research'
    reopened_derived = None
    with ArchiveStore(path) as store:
        reopened_derived = CaptureReader(store).read_derived('research')
        derived_check.check_result(reopened_derived, 'research', parents,
                                   derived_original, derived_expected)
        assert EvidenceAuditor(store).audit_derived('research') is True
        if not research.exists():
            CaptureExporter(store).export_derived('research', research)
        derived_manifest = json.loads((research / 'manifest.json').read_text())
        assert derived_manifest['uid_map'] == derived_check.UID_MAP
        for entry, sample in zip(derived_manifest['files'], derived_expected):
            assert (research / entry['filename']).read_bytes() == sample['body']
        assert b'APP literal 1.2.3 must stay' in reopened_derived['files'][0]['body']
        assert b'APP literal 2.25.12345 must stay' not in reopened_derived['files'][0]['body']
        files = reopened_derived['files']
        # Locate the Pixel Data element header in each full Part 10 byte string.
        from acquisition.dataset.metadata import parse_metadata

        def pixel_start(body):
            return parse_metadata(body)[1].value_start - 12
        print(json.dumps({
            'receipt': receipt.as_dict(),
            'audit': [asdict(result) for result in audits],
            'delivery': str(target),
            'derived': {
                'package_id': 'research',
                'uid_map': derived_check.UID_MAP,
                'parents': [entry['parent_capture_id'] for entry in files],
                'derived_digests': [entry['derived_digest'] for entry in files],
                'sizes': [{'parent': len(sample['body']), 'derived': len(entry['body']),
                           'parent_pixel_start': sample['pixel_start'],
                           'derived_pixel_start': pixel_start(entry['body'])}
                          for entry, sample in zip(files, derived_original)],
                'app_literal_preserved': [
                    b'APP literal 1.2.3 must stay' in entry['body'] for entry in files],
                'patient_name_removed': [
                    b'INNER^NAME' not in entry['body'] and b'patient-7' not in entry['body']
                    for entry in files],
                'source_spans': [entry['frames'][0]['source_spans'] for entry in files],
                'derived_spans': [entry['frames'][0]['derived_spans'] for entry in files],
                'reopened': True,
                'delivery': str(research),
            }}, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline DICOM frame acquisition')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('verify')
    demo_parser = commands.add_parser('demo')
    demo_parser.add_argument('--directory', required=True)
    for command in ('ingest', 'read', 'audit', 'export'):
        child = commands.add_parser(command)
        child.add_argument('--db', required=True)
        if command == 'ingest':
            child.add_argument('--source-id', required=True)
            child.add_argument('file')
        else:
            child.add_argument('capture_id')
        if command == 'read':
            child.add_argument('--frame', type=int)
        if command == 'export':
            child.add_argument('destination')
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            return verify()
        if args.command == 'demo':
            return demo(args.directory)
        with ArchiveStore(args.db) as store:
            if args.command == 'ingest':
                output = CaptureService(store).ingest(args.source_id, Path(args.file).read_bytes()).as_dict()
            elif args.command == 'read':
                result = CaptureReader(store).read(args.capture_id, args.frame)
                output = result if args.frame is None else {
                    'index': result.index, 'body_hex': result.body.hex(),
                    'digest': result.digest, 'spans': [[s.start, s.end] for s in result.spans]}
            elif args.command == 'audit':
                output = [asdict(result) for result in EvidenceAuditor(store).audit(args.capture_id)]
            else:
                output = CaptureExporter(store).export(args.capture_id, args.destination).value
            print(json.dumps(output, sort_keys=True))
        return 0
    except (CaptureError, NotFound, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
