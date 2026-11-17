import struct
import unittest
from hashlib import sha256
from acquisition.archive.store import ArchiveStore
from acquisition.archive.read import CaptureReader
from acquisition.delivery.batch import CaptureService
from acquisition.receipt.source import CaptureError
from acquisition.dataset.metadata import parse_metadata
from acquisition.encapsulation.items import read_items
from acquisition.encapsulation.payload import build_payload_view
from acquisition.encapsulation.provenance import project_range, rebuild_bytes
from tests.fixtures import make_case, jpeg, element, replace_bytes, scenario


class TestFrames(unittest.TestCase):
    def assert_case(self, case, identity='case'):
        with ArchiveStore(':memory:') as store:
            receipt = CaptureService(store).ingest(identity, case.body)
            reader = CaptureReader(store)
            for index, expected in enumerate(case.frames):
                frame = reader.read(receipt.capture_id, index)
                self.assertEqual(frame.body, expected)
                self.assertEqual(frame.digest, sha256(expected).hexdigest())
                spans = tuple((span.start, span.end) for span in frame.spans)
                self.assertEqual(spans, case.spans[index])
                self.assertEqual(b''.join(case.body[a:b] for a, b in spans), expected)
            return [reader.read(receipt.capture_id, i).digest for i in range(len(case.frames))]

    def test_offset_and_empty_table(self):
        left = make_case(table=True)
        right = make_case(table=False)
        self.assertEqual(self.assert_case(left), self.assert_case(right))
        offset = struct.unpack_from('<I', left.body, left.bot_start + 4)[0]
        expected = left.item_positions[len(left.chunks) // 2] - left.item_positions[0]
        self.assertEqual(offset, expected)
        self.assertGreater(offset, len(left.frames[0]))
        for odd in (False, True):
            self.assert_case(make_case(count=1, frames=(jpeg(3, odd=odd),), split=(2, 8, 26)))
        metadata, location = parse_metadata(left.body)
        view = build_payload_view(left.body, read_items(left.body, location))
        for start, end in ((0, 1), (1, 3), (2, 6), (5, 7), (6, 7), (19, 21)):
            spans = project_range(view, start, end)
            self.assertEqual(rebuild_bytes(left.body, spans), view.body[start:end])
        scenario('v3', {'bot_second_offset': offset, 'item_header_size': 8},
                 [sha256(frame).hexdigest() for frame in left.frames],
                 self.assert_case(left), True,
                 all(b''.join(left.body[a:b] for a, b in spans) == frame
                     for spans, frame in zip(left.spans, left.frames)),
                 ['acquisition/encapsulation/items.py', 'acquisition/encapsulation/offsets.py',
                  'acquisition/encapsulation/provenance.py', 'acquisition/frames/extract.py'])

    def test_fragmentation_invariance(self):
        frames = (jpeg(4, odd=True), jpeg(5, odd=False))
        self.assertIn(b'\xff\0', frames[0])
        self.assertIn(b'\xff\xff\xd0', frames[0])
        self.assertGreater(frames[0].count(b'\xff\xd9'), 1)
        expected = [sha256(frame).hexdigest() for frame in frames]
        # Every even cut in the complete stream, including SOI/EOI and segment lengths.
        stream_size = sum(map(len, frames))
        for cut in range(2, stream_size, 2):
            case = make_case(table=False, frames=frames, packed=True, split=(cut,))
            with self.subTest(cut=cut):
                self.assertEqual(self.assert_case(case), expected)
        for split in ((), (2, 4, 6), tuple(range(2, 240, 2))):
            self.assertEqual(self.assert_case(make_case(frames=frames, split=split)), expected)

    def test_invalid_capture_rejected(self):
        valid = make_case(nested=True)
        variants = []
        for declaration in (b'', b'0', b'-1', b'1.5', b'2junk', b'1', b'3'):
            variants.append(make_case(declared=declaration).body)
        for tag, vr, value in [
                ((0x28, 0x100), 'US', struct.pack('<H', 16)),
                ((0x28, 0x10), 'US', b'\0\0'),
                ((0x28, 2), 'US', b'\x02\0'),
                ((0x28, 0x101), 'US', b'\x07\0'),
                ((0x28, 0x10), 'US', b'\x08\0\0\0'),
                ((8, 0x18), 'UI', b'2.025.1'),
                ((0x28, 4), 'CS', b'INVALID')]:
            variants.append(make_case(overrides={tag: (vr, value)}).body)
        syntax = b'1.2.840.10008.1.2.4.50'
        variants.append(valid.body.replace(syntax, syntax[:-2] + b'70', 1))
        variants.extend([valid.body[:-1], valid.body[:180], valid.body[:-8]])
        variants.append(valid.body.replace(element(0x28, 0x10, 'US', struct.pack('<H', 8)), b'', 1))
        variants.append(valid.body + element(0x28, 0x10, 'SQ', b''))
        # Delimiters, item and table widths and top-level contradictions.
        variants.append(replace_bytes(valid.body, valid.body.index(b'\xfe\xff\x0d\xe0'), b'\xfe\xff\xdd\xe0'))
        sq_header = valid.body.index(b'\x08\0\x15\x11SQ')
        variants.append(replace_bytes(valid.body, sq_header + 8, struct.pack('<I', 0xfffffffe)))
        for length in (1, 0xffffffff, 0xfffffff0):
            variants.append(replace_bytes(valid.body, valid.item_positions[0] + 4, struct.pack('<I', length)))
        variants.append(replace_bytes(valid.body, valid.pixel_start + 4, struct.pack('<I', 2)))
        variants.append(valid.body + element(0x28, 0x10, 'US', struct.pack('<H', 9)))
        for offsets in ([0, 1], [0, 8], [0, 10], [0, 0], [0], [0, 0xffffffff],
                        [0, valid.item_positions[1] - valid.item_positions[0]]):
            variants.append(make_case(offset_override=offsets).body)
        frame = jpeg(0, odd=False)
        bad_frames = [frame[:-2], frame.replace(b'\xff\xe1', b'\xff\xd8', 1),
                      replace_bytes(frame, 4, b'\xff\xff'),
                      frame.replace(b'\xff\xff\xd0', b'\xff\xff\xd3', 1),
                      frame.replace(b'\xff\xdb', b'\xff\xc2', 1),
                      frame.replace(b'\xff\0', b'\xff\xff\0', 1),
                      frame[:-3] + b'\xff']
        variants.extend(make_case(frames=(bad,), table=False).body for bad in bad_frames)
        variants.extend(make_case(count=1, suffix=tail).body for tail in (b'\0\0', b'xx', b'\xff\xff'))
        with ArchiveStore(':memory:') as store:
            service = CaptureService(store)
            old = service.ingest('old', valid.body)
            before = CaptureReader(store).read(old.capture_id)
            for i, body in enumerate(variants):
                with self.subTest(variant=i):
                    with self.assertRaises(CaptureError):
                        service.ingest('invalid-' + str(i), body)
                    self.assertIsNone(store.load_receipt('invalid-' + str(i)))
                    self.assertEqual(CaptureReader(store).read(old.capture_id), before)
                    for table in ('sources', 'captures', 'receipts'):
                        self.assertEqual(store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
