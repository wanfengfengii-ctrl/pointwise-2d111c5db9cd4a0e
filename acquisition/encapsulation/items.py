from dataclasses import dataclass
from acquisition.receipt.source import CaptureError
from acquisition.dataset.elements import ElementReader, UNDEFINED


@dataclass(frozen=True)
class FragmentItem:
    index: int
    tag_position: int
    payload_start: int
    payload_end: int

    @property
    def length(self):
        return self.payload_end - self.payload_start


@dataclass(frozen=True)
class EncapsulationDirectory:
    offset_bytes: bytes
    fragments: tuple
    end: int


def read_items(data, location):
    reader = ElementReader(data)
    tag, length, start = reader.item_header(location.value_start, location.end)
    if tag != 0xe000 or length == UNDEFINED or length % 4 or start + length > location.end:
        raise CaptureError('invalid basic offset table item')
    table = data[start:start + length]
    position = start + length
    fragments = []
    while True:
        tag, length, start = reader.item_header(position, location.end)
        if tag == 0xe0dd:
            if length != 0 or start != location.end or not fragments:
                raise CaptureError('invalid encapsulation termination')
            return EncapsulationDirectory(table, tuple(fragments), start)
        if tag != 0xe000 or length == UNDEFINED or length == 0 or length % 2:
            raise CaptureError('fragment requires positive even explicit length')
        if start + length > location.end:
            raise CaptureError('fragment outside Pixel Data')
        fragments.append(FragmentItem(len(fragments), position, start, start + length))
        position = start + length
