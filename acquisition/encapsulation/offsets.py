from dataclasses import dataclass
import struct
from acquisition.receipt.source import CaptureError


@dataclass(frozen=True)
class OffsetTable:
    item_offsets: tuple
    logical_starts: tuple


def parse_offset_table(directory, frame_count):
    raw = directory.offset_bytes
    if len(raw) % 4:
        raise CaptureError('offset table width')
    offsets = tuple(value[0] for value in struct.iter_unpack('<I', raw))
    if offsets:
        if len(offsets) != frame_count or offsets[0] != 0:
            raise CaptureError('offset table count or origin')
        if any(a >= b for a, b in zip(offsets, offsets[1:])):
            raise CaptureError('offset table must strictly increase')
    return OffsetTable(offsets, resolve_item_offsets(directory, offsets))


def resolve_item_offsets(directory, offsets):
    origin = directory.fragments[0].tag_position
    mapping = {}
    logical = 0
    for fragment in directory.fragments:
        mapping[fragment.tag_position - origin] = logical
        logical += fragment.length
    try:
        return tuple(mapping[offset] for offset in offsets)
    except KeyError as exc:
        raise CaptureError('offset must point to a fragment item tag') from exc


def validate_frame_starts(table, starts):
    if table.item_offsets and tuple(starts) != table.logical_starts:
        raise CaptureError('offset table disagrees with JPEG frame starts')
