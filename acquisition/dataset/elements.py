from dataclasses import dataclass
import struct
from acquisition.receipt.source import CaptureError

LONG_VRS = {'OB', 'OD', 'OF', 'OL', 'OV', 'OW', 'SQ', 'UC', 'UR', 'UT', 'UN'}
VRS = LONG_VRS | {'AE', 'AS', 'AT', 'CS', 'DA', 'DS', 'DT', 'FD', 'FL',
                 'IS', 'LO', 'LT', 'PN', 'SH', 'SL', 'SS', 'ST', 'SV',
                 'TM', 'UI', 'UL', 'US', 'UV'}
UNDEFINED = 0xffffffff


@dataclass(frozen=True)
class Element:
    tag: tuple
    vr: str
    value_start: int
    length: int

    @property
    def end(self):
        return self.value_start + self.length


def read_file_header(data):
    if len(data) < 132 or data[128:132] != b'DICM':
        raise CaptureError('DICOM Part 10 header required')
    return 132


class ElementReader:
    def __init__(self, data):
        self.data = data

    def read(self, position, limit=None):
        limit = len(self.data) if limit is None else limit
        if not 0 <= position <= limit <= len(self.data) or position + 8 > limit:
            raise CaptureError('truncated element header')
        group, number = struct.unpack_from('<HH', self.data, position)
        try:
            vr = self.data[position + 4:position + 6].decode('ascii')
        except UnicodeError as exc:
            raise CaptureError('invalid VR') from exc
        if group == 0xfffe or vr not in VRS:
            raise CaptureError('unexpected delimiter or VR')
        if vr in LONG_VRS:
            if position + 12 > limit or self.data[position + 6:position + 8] != b'\0\0':
                raise CaptureError('invalid long VR header')
            length = struct.unpack_from('<I', self.data, position + 8)[0]
            start = position + 12
        else:
            length = struct.unpack_from('<H', self.data, position + 6)[0]
            start = position + 8
        if length != UNDEFINED and (length % 2 or start + length > limit):
            raise CaptureError('element length exceeds boundary or is odd')
        if length == UNDEFINED and vr != 'SQ' and (group, number) != (0x7fe0, 0x10):
            raise CaptureError('undefined length outside SQ or Pixel Data')
        return Element((group, number), vr, start, length)

    def item_header(self, position, limit=None):
        limit = len(self.data) if limit is None else limit
        if position < 0 or position + 8 > limit or limit > len(self.data):
            raise CaptureError('truncated item header')
        group, tag, length = struct.unpack_from('<HHI', self.data, position)
        if group != 0xfffe:
            raise CaptureError('item tag expected')
        return tag, length, position + 8

    def encapsulated_end(self, position):
        while True:
            tag, length, start = self.item_header(position)
            if tag == 0xe0dd:
                if length != 0:
                    raise CaptureError('nonzero sequence delimiter')
                return start
            if tag != 0xe000 or length == UNDEFINED or length % 2 or start + length > len(self.data):
                raise CaptureError('invalid encapsulated item')
            position = start + length
