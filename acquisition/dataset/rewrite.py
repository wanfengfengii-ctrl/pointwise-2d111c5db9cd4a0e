"""Byte-preserving limited rewrite for fixed research copies.

Only the three target tags below are changed (patient identity elements are
removed).  Every other element, item, delimiter and the whole encapsulated
Pixel Data element keep their original bytes and order; only enclosing
container lengths (bounded SQ / item, UI element length, file meta group
length) are recomputed.  The bounded/undefined length form of every container
is preserved.
"""
import struct

from acquisition.receipt.source import CaptureError
from acquisition.dataset.elements import ElementReader, UNDEFINED, read_file_header
from acquisition.dataset.sequences import SequenceWalker
from acquisition.dataset.values import decode_uid, valid_uid

DELETE_TAGS = {(0x0010, 0x0010), (0x0010, 0x0020)}
UID_TAGS = {(0x0008, 0x0018), (0x0008, 0x1155)}
MEDIA_UID_TAG = (0x0002, 0x0003)
GROUP_LENGTH_TAG = (0x0002, 0x0000)
PIXEL_TAG = (0x7fe0, 0x0010)


def encode_ui(text):
    raw = text.encode('ascii')
    return raw + (b'\0' if len(raw) % 2 else b'')


def validate_uid_map(uid_map):
    if not isinstance(uid_map, dict):
        raise CaptureError('uid_map must be a dict')
    result = {}
    for old, new in uid_map.items():
        if not isinstance(old, str) or not isinstance(new, str):
            raise CaptureError('uid_map keys and values must be strings')
        if not valid_uid(old):
            raise CaptureError('uid_map key is not a UID')
        if not valid_uid(new):
            raise CaptureError('uid_map value is not a valid UID of at most 64 characters')
        result[old] = new
    if len(set(result.values())) != len(result):
        raise CaptureError('uid_map values must be distinct')
    return result


class _Scope:
    def __init__(self, data, uid_map):
        self.data = data
        self.reader = ElementReader(data)
        self.walker = SequenceWalker(self.reader)
        self.uid_map = uid_map
        self.seen = set()

    def extent(self, element, limit):
        if element.vr == 'SQ' and element.length == UNDEFINED:
            return self.walker.skip_sequence(element, limit)
        return element.end

    def elements(self, position, end, limit):
        out = bytearray()
        while True:
            if end is not None:
                if position == end:
                    return bytes(out), position
                if position > end:
                    raise CaptureError('element scope overrun')
            else:
                if position + 2 > limit:
                    raise CaptureError('unterminated item')
                if self.data[position:position + 2] == b'\xfe\xff':
                    tag, length, start = self.reader.item_header(position, limit)
                    if tag != 0xe00d or length != 0:
                        raise CaptureError('unexpected delimiter in item')
                    return bytes(out), position
            ceiling = limit if end is None else min(end, limit)
            element = self.reader.read(position, ceiling)
            tag = element.tag
            if tag in DELETE_TAGS:
                # The entire element (and anything nested in it) is removed;
                # deletion is by tag, never by VR or text guessing.
                position = self.extent(element, ceiling)
                continue
            if tag in UID_TAGS:
                if element.vr != 'UI':
                    raise CaptureError('target UID element must be single-value UI')
                old = decode_uid(self.data, element)
                if old not in self.uid_map:
                    raise CaptureError('target UID is not covered by uid_map')
                self.seen.add(old)
                value = encode_ui(self.uid_map[old])
                out += (self.data[position:position + 6]
                        + struct.pack('<H', len(value)) + value)
                position = element.end
                continue
            if tag == PIXEL_TAG:
                if element.vr != 'OB' or element.length != UNDEFINED:
                    raise CaptureError('encapsulated Pixel Data required')
                pixel_end = self.reader.encapsulated_end(element.value_start)
                if pixel_end > ceiling:
                    raise CaptureError('Pixel Data exceeds scope')
                out += self.data[position:pixel_end]
                position = pixel_end
                continue
            if element.vr == 'SQ':
                content = self.sequence(element, ceiling)
                if element.length == UNDEFINED:
                    out += self.data[position:element.value_start] + content
                else:
                    out += (self.data[position:position + 8]
                            + struct.pack('<I', len(content)) + content)
                position = self.extent(element, ceiling)
                continue
            out += self.data[position:element.end]
            position = element.end

    def sequence(self, element, limit):
        position = element.value_start
        undefined = element.length == UNDEFINED
        end = None if undefined else element.value_start + element.length
        out = bytearray()
        while True:
            if end is not None and position == end:
                return bytes(out)
            if end is not None and position > end:
                raise CaptureError('sequence overrun')
            ceiling = limit if end is None else min(end, limit)
            tag, length, start = self.reader.item_header(position, ceiling)
            if tag == 0xe0dd:
                if not undefined or length != 0 or start > limit:
                    raise CaptureError('unexpected sequence delimiter')
                out += self.data[position:start]
                return bytes(out)
            if tag != 0xe000:
                raise CaptureError('sequence requires items')
            if length == UNDEFINED:
                content, new_position = self.elements(start, None, ceiling)
                out += (self.data[position:start] + content
                        + self.data[new_position:new_position + 8])
                position = new_position + 8
            else:
                item_end = start + length
                if length % 2 or item_end > ceiling:
                    raise CaptureError('item exceeds sequence')
                content, new_position = self.elements(start, item_end, item_end)
                out += (self.data[position:position + 4]
                        + struct.pack('<I', len(content)) + content)
                position = new_position


def _rewrite_meta(data, position, uid_map):
    reader = ElementReader(data)
    entries = []
    while position + 4 <= len(data) and data[position:position + 2] == b'\x02\0':
        element = reader.read(position)
        if element.vr == 'SQ' or element.length == UNDEFINED:
            raise CaptureError('invalid file meta element')
        entries.append((position, element))
        position = element.end
    if not entries:
        raise CaptureError('file meta group required')
    group_seen = False
    seen = set()
    pieces = []
    for start, element in entries:
        if element.tag == GROUP_LENGTH_TAG:
            if group_seen:
                raise CaptureError('duplicate file meta group length')
            if element.vr != 'UL' or element.length != 4:
                raise CaptureError('invalid file meta group length')
            group_seen = True
            pieces.append(None)
            continue
        if element.tag == MEDIA_UID_TAG:
            if element.vr != 'UI':
                raise CaptureError('media storage instance UID must be single-value UI')
            old = decode_uid(data, element)
            if old not in uid_map:
                raise CaptureError('media storage instance UID is not covered by uid_map')
            seen.add(old)
            value = encode_ui(uid_map[old])
            pieces.append(data[start:start + 6] + struct.pack('<H', len(value)) + value)
        else:
            pieces.append(data[start:element.end])
    if group_seen:
        index = pieces.index(None)
        following = sum(len(piece) for piece in pieces[index + 1:])
        start = entries[index][0]
        value_start = entries[index][1].value_start
        pieces[index] = data[start:value_start] + struct.pack('<I', following)
    return b''.join(pieces), position, seen


def rewrite_file(data, uid_map):
    """Return (derived Part 10 bytes, set of old target UIDs encountered)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CaptureError('file bytes must be bytes-like')
    data = bytes(data)
    mapping = validate_uid_map(uid_map)
    start = read_file_header(data)
    meta, position, seen = _rewrite_meta(data, start, mapping)
    scope = _Scope(data, mapping)
    dataset, _ = scope.elements(position, len(data), len(data))
    seen |= scope.seen
    return data[:start] + meta + dataset, seen
