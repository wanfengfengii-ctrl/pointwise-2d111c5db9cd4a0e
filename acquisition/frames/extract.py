from dataclasses import dataclass
from hashlib import sha256
import struct
from acquisition.receipt.source import CaptureError
from acquisition.encapsulation.provenance import project_range
from acquisition.encapsulation.offsets import validate_frame_starts
from acquisition.frames.markers import (read_marker, read_segment,
    validate_baseline_header, parse_quantization, parse_huffman)
from acquisition.frames.entropy import scan_entropy


@dataclass(frozen=True)
class FrameBoundary:
    start: int
    end: int


@dataclass(frozen=True)
class CapturedFrame:
    index: int
    body: bytes
    digest: str
    spans: tuple
    boundary: FrameBoundary


def _frame_end(data, start, metadata):
    if data[start:start + 2] != b'\xff\xd8':
        raise CaptureError('frame must begin with SOI')
    position = start + 2
    components = None
    scanned = set()
    quantization = set()
    huffman = set()
    interval = 0
    while True:
        marker = read_marker(data, position)
        code = marker.code
        if code == 0xd9:
            if components is None or scanned != set(components):
                raise CaptureError('EOI before complete Baseline scans')
            return marker.end
        if code in {0xd8, 0x01} or 0xd0 <= code <= 0xd7:
            raise CaptureError('illegal standalone marker')
        payload, position = read_segment(data, marker)
        if code == 0xc0:
            if components is not None or scanned:
                raise CaptureError('duplicate or late SOF0')
            components = validate_baseline_header(payload, metadata)
        elif code == 0xdb:
            quantization.update(parse_quantization(payload))
        elif code == 0xc4:
            huffman.update(parse_huffman(payload))
        elif code == 0xdd:
            if len(payload) != 2:
                raise CaptureError('invalid DRI length')
            interval = struct.unpack('>H', payload)[0]
        elif code == 0xda:
            if components is None or not payload:
                raise CaptureError('SOS before SOF0')
            count = payload[0]
            if not 1 <= count <= len(components) or len(payload) != 1 + 2 * count + 3:
                raise CaptureError('invalid SOS length')
            if payload[-3:] != b'\0\x3f\0':
                raise CaptureError('non-Baseline scan parameters')
            current = set()
            for i in range(count):
                identity, tables = payload[1 + 2 * i:3 + 2 * i]
                if identity not in components or identity in current or identity in scanned:
                    raise CaptureError('duplicate or unknown scan component')
                if (components[identity] not in quantization
                        or (0, tables >> 4) not in huffman or (1, tables & 15) not in huffman):
                    raise CaptureError('scan references missing coding table')
                current.add(identity)
            scanned.update(current)
            boundary = scan_entropy(data, position, interval)
            if boundary.marker_position == position:
                raise CaptureError('empty entropy scan')
            position = boundary.marker_position
        elif not (0xe0 <= code <= 0xef or code == 0xfe):
            raise CaptureError('unsupported JPEG marker for Baseline')


def extract_frames(view, metadata, offset_table):
    frames = []
    position = 0
    while position < len(view.body):
        start = position
        end = _frame_end(view.body, start, metadata)
        body = view.read(start, end)
        spans = project_range(view, start, end)
        frames.append(CapturedFrame(len(frames), body, sha256(body).hexdigest(),
                                    spans, FrameBoundary(start, end)))
        position = end
        if (position < len(view.body) and view.body[position] == 0
                and view.is_item_end(position + 1)):
            position += 1
        if len(frames) > metadata.frame_count:
            raise CaptureError('too many JPEG frames')
    if len(frames) != metadata.frame_count:
        raise CaptureError('declared frame count differs from JPEG frames')
    validate_frame_starts(offset_table, [frame.boundary.start for frame in frames])
    return tuple(frames)
