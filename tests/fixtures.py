from dataclasses import dataclass
from hashlib import sha256
import json
import struct


def element(group, tag, vr, value, undefined=False):
    if not undefined and len(value) % 2:
        value += b'\0' if vr in {'UI', 'OB'} else b' '
    head = struct.pack('<HH', group, tag) + vr.encode('ascii')
    if vr in {'OB', 'OW', 'SQ', 'UN', 'UT'}:
        head += b'\0\0' + struct.pack('<I', 0xffffffff if undefined else len(value))
    else:
        head += struct.pack('<H', len(value))
    return head + value


def item(value, undefined=False):
    return struct.pack('<HHI', 0xfffe, 0xe000, 0xffffffff if undefined else len(value)) + value


def delimiter(tag):
    return struct.pack('<HHI', 0xfffe, tag, 0)


def segment(code, body):
    return b'\xff' + bytes([code]) + struct.pack('>H', len(body) + 2) + body


def jpeg(seed=0, odd=None):
    app = b'offline' + bytes([seed % 128]) + b'\xff\xd9inside-app'
    quantization = bytes([0]) + bytes([1]) * 64
    # DC category zero: code 0. AC 0A: code 0, EOB: code 10.
    huffman = (bytes([0, 1]) + bytes(15) + bytes([0])
               + bytes([0x10, 1, 1]) + bytes(14) + bytes([0x0a, 0]))
    sof = struct.pack('>BHHB', 8, 8, 32, 1) + b'\x01\x11\0'
    bits = ('00' + '1' * 10 + '10') * 2
    bits += '1' * ((-len(bits)) % 8)
    scan = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    scan = scan.replace(b'\xff', b'\xff\0')
    def build(app_body):
        return (b'\xff\xd8' + segment(0xe1, app_body)
                + segment(0xfe, b'comment\xff\xd9payload')
                + segment(0xdb, quantization) + segment(0xc0, sof)
                + segment(0xc4, huffman) + segment(0xdd, b'\0\x02')
                + segment(0xda, b'\x01\x01\0\0\x3f\0')
                + scan + b'\xff\xff\xd0' + scan + b'\xff\xff\xd9')
    result = build(app)
    if odd is not None and bool(len(result) % 2) != odd:
        result = build(app + b'x')
    return result


def nested_sequence():
    inner = element(0x28, 8, 'IS', b'999')
    bounded = element(0x0008, 0x1115, 'SQ', item(inner))
    unbounded = item(bounded + element(0x28, 8, 'IS', b'777'), True) + delimiter(0xe00d)
    return element(0x0008, 0x1111, 'SQ', unbounded + delimiter(0xe0dd), True)


@dataclass(frozen=True)
class Case:
    body: bytes
    frames: tuple
    spans: tuple
    item_positions: tuple
    bot_start: int
    pixel_start: int
    logical_starts: tuple
    chunks: tuple


def make_case(count=2, table=True, split=(2, 6, 20, 54, 104, 150),
              nested=False, declared='auto', overrides=None, frames=None,
              packed=False, suffix=b'', offset_override=None):
    frames = tuple(jpeg(i, odd=bool(i % 2)) for i in range(count)) if frames is None else tuple(frames)
    count = len(frames)
    chunks = []
    starts = []
    logical = 0
    frame_first_items = []
    if packed:
        if table:
            raise ValueError('packed layout uses empty BOT')
        stream = b''.join(frames)
        for frame in frames:
            starts.append(logical)
            logical += len(frame)
        if len(stream) % 2:
            stream += b'\0'
        cuts = sorted({value for value in split if 0 < value < len(stream) and value % 2 == 0})
        positions = [0] + cuts + [len(stream)]
        chunks = [stream[a:b] for a, b in zip(positions, positions[1:])]
    else:
        for frame in frames:
            starts.append(logical)
            frame_first_items.append(len(chunks))
            padded = frame + (b'\0' if len(frame) % 2 else b'')
            cuts = sorted({value for value in split if 0 < value < len(padded) and value % 2 == 0})
            positions = [0] + cuts + [len(padded)]
            for a, b in zip(positions, positions[1:]):
                chunks.append(padded[a:b])
            logical += len(padded)
    if suffix:
        chunks[-1] += suffix
    image = {
        (8, 0x16): ('UI', b'1.2.840.10008.5.1.4.1.1.7'),
        (8, 0x18): ('UI', b'2.25.123456'),
        (0x28, 2): ('US', struct.pack('<H', 1)),
        (0x28, 4): ('CS', b'MONOCHROME2'),
        (0x28, 0x10): ('US', struct.pack('<H', 8)),
        (0x28, 0x11): ('US', struct.pack('<H', 32)),
        (0x28, 0x100): ('US', struct.pack('<H', 8)),
        (0x28, 0x101): ('US', struct.pack('<H', 8)),
        (0x28, 0x102): ('US', struct.pack('<H', 7)),
        (0x28, 0x103): ('US', struct.pack('<H', 0))}
    if declared is not None:
        image[(0x28, 8)] = ('IS', str(count).encode() if declared == 'auto' else declared)
    if overrides:
        image.update(overrides)
    syntax = element(2, 0x10, 'UI', b'1.2.840.10008.1.2.4.50')
    meta = element(2, 0, 'UL', struct.pack('<I', len(syntax))) + syntax
    prefix = bytes(128) + b'DICM' + meta
    if nested:
        prefix += nested_sequence()
    prefix += element(0x19, 0x10, 'LO', b'ignored normal value')
    for (group, tag), (vr, value) in sorted(image.items()):
        prefix += element(group, tag, vr, value)
    if nested:
        prefix += element(0x19, 0x20, 'SQ', item(nested_sequence()))
    prefix += element(0x7fe0, 0x10, 'OB', b'', True)
    pixel_start = len(prefix)
    relative_tags = []
    relative = 0
    for chunk in chunks:
        relative_tags.append(relative)
        relative += 8 + len(chunk)
    offsets = ([relative_tags[index] for index in frame_first_items] if table else [])
    if offset_override is not None:
        offsets = offset_override
    bot = b''.join(struct.pack('<I', offset) for offset in offsets)
    body = prefix + item(bot)
    bot_start = pixel_start + 8
    logical_to_source = []
    item_positions = []
    for chunk in chunks:
        item_positions.append(len(body))
        payload_start = len(body) + 8
        logical_to_source.extend(range(payload_start, payload_start + len(chunk)))
        body += item(chunk)
    body += delimiter(0xe0dd)
    expected_spans = []
    # Independent byte-address enumeration, not the production interval-intersection algorithm.
    for start, frame in zip(starts, frames):
        addresses = logical_to_source[start:start + len(frame)]
        spans = []
        for address in addresses:
            if spans and spans[-1][1] == address:
                spans[-1][1] += 1
            else:
                spans.append([address, address + 1])
        expected_spans.append(tuple(tuple(pair) for pair in spans))
    return Case(body, frames, tuple(expected_spans), tuple(item_positions),
                bot_start, pixel_start, tuple(starts), tuple(chunks))


def replace_bytes(body, position, value):
    return body[:position] + value + body[position + len(value):]


def scenario(case_id, inputs, expected, actual, downstream_expected, downstream_actual, paths):
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id, 'input': inputs, 'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': paths,
        'oracle': 'independent fixture byte-address enumeration and SHA-256 of original frame constants'
    }, sort_keys=True))
