from dataclasses import asdict, dataclass
import struct
from acquisition.receipt.source import CaptureError
from acquisition.dataset.elements import ElementReader, UNDEFINED, read_file_header
from acquisition.dataset.sequences import SequenceWalker
from acquisition.dataset.values import decode_uid, decode_us, decode_text, decode_positive_is

TRANSFER_SYNTAX = '1.2.840.10008.1.2.4.50'
US_TAGS = {0x10: 'rows', 0x11: 'columns', 2: 'samples_per_pixel',
           0x100: 'bits_allocated', 0x101: 'bits_stored',
           0x102: 'high_bit', 0x103: 'pixel_representation', 6: 'planar_configuration'}


@dataclass(frozen=True)
class ImageMetadata:
    sop_class_uid: str
    sop_instance_uid: str
    rows: int
    columns: int
    samples_per_pixel: int
    photometric: str
    bits_allocated: int
    bits_stored: int
    high_bit: int
    pixel_representation: int
    frame_count: int
    planar_configuration: int = 0
    transfer_syntax: str = TRANSFER_SYNTAX

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PixelDataLocation:
    value_start: int
    end: int


def validate_metadata(value):
    fields = set(ImageMetadata.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) != fields:
        raise CaptureError('metadata fields mismatch')
    for key in fields - {'sop_class_uid', 'sop_instance_uid', 'photometric', 'transfer_syntax'}:
        if type(value[key]) is not int:
            raise CaptureError('metadata integer required')
    for key in ('sop_class_uid', 'sop_instance_uid'):
        text = value[key]
        if not isinstance(text, str) or not text or any(
                not part.isascii() or not part.isdigit() or (len(part) > 1 and part[0] == '0')
                for part in text.split('.')):
            raise CaptureError('metadata UID invalid')
    if value['transfer_syntax'] != TRANSFER_SYNTAX:
        raise CaptureError('unsupported transfer syntax')
    if not (0 < value['rows'] <= 65535 and 0 < value['columns'] <= 65535 and value['frame_count'] > 0):
        raise CaptureError('invalid dimensions or frame count')
    if (value['bits_allocated'], value['bits_stored'], value['high_bit'],
            value['pixel_representation'], value['planar_configuration']) != (8, 8, 7, 0, 0):
        raise CaptureError('only unsigned 8-bit interleaved images supported')
    samples = value['samples_per_pixel']
    allowed = {1: {'MONOCHROME1', 'MONOCHROME2'}, 3: {'RGB', 'YBR_FULL', 'YBR_FULL_422'}}
    if samples not in allowed or value['photometric'] not in allowed[samples]:
        raise CaptureError('unsupported sampling/photometric combination')


def parse_metadata(data):
    reader = ElementReader(data)
    walker = SequenceWalker(reader)
    position = read_file_header(data)
    meta = {}
    meta_end = None
    while position + 4 <= len(data) and data[position:position + 2] == b'\x02\0':
        element = reader.read(position)
        if element.length == UNDEFINED or element.vr == 'SQ' or element.tag in meta:
            raise CaptureError('invalid file meta element')
        meta[element.tag] = element
        if element.tag == (2, 0):
            if element.vr != 'UL' or element.length != 4:
                raise CaptureError('invalid file meta group length')
            meta_end = element.end + struct.unpack_from('<I', data, element.value_start)[0]
        position = element.end
    if meta_end is not None and meta_end != position:
        raise CaptureError('file meta group length mismatch')
    if (2, 0x10) not in meta or decode_uid(data, meta[(2, 0x10)]) != TRANSFER_SYNTAX:
        raise CaptureError('JPEG Baseline transfer syntax required')
    values = {}
    pixel = None
    def bind(key, value):
        if key in values and values[key] != value:
            raise CaptureError('contradictory required tag: ' + key)
        values[key] = value
    while position < len(data):
        element = reader.read(position)
        if element.tag[0] == 2 or element.tag in {(0x7fe0, 1), (0x7fe0, 2)}:
            raise CaptureError('misplaced meta or unsupported extended offsets')
        if element.vr == 'SQ':
            if element.tag in {(8, 0x16), (8, 0x18), (0x28, 4), (0x28, 8), (0x7fe0, 0x10)} or (element.tag[0] == 0x28 and element.tag[1] in US_TAGS):
                raise CaptureError('required image tag cannot be SQ')
            position = walker.skip_sequence(element)
            continue
        if element.tag == (0x7fe0, 0x10):
            if pixel is not None or element.vr != 'OB' or element.length != UNDEFINED:
                raise CaptureError('undefined OB encapsulated Pixel Data required')
            end = reader.encapsulated_end(element.value_start)
            pixel = PixelDataLocation(element.value_start, end)
            position = end
            continue
        if element.tag in {(8, 0x16), (8, 0x18)}:
            bind('sop_class_uid' if element.tag[1] == 0x16 else 'sop_instance_uid',
                 decode_uid(data, element))
        elif element.tag[0] == 0x28 and element.tag[1] in US_TAGS:
            bind(US_TAGS[element.tag[1]], decode_us(data, element))
        elif element.tag == (0x28, 4):
            bind('photometric', decode_text(data, element, 'CS').rstrip(' '))
        elif element.tag == (0x28, 8):
            bind('frame_count', decode_positive_is(data, element))
        position = element.end
    if pixel is None:
        raise CaptureError('missing Pixel Data')
    values.setdefault('frame_count', 1)
    values.setdefault('planar_configuration', 0)
    required = set(ImageMetadata.__dataclass_fields__) - {'transfer_syntax'}
    if set(values) != required:
        raise CaptureError('missing required image metadata')
    metadata = ImageMetadata(**values)
    validate_metadata(metadata.as_dict())
    for tag, key in ((0x2, 'sop_class_uid'), (0x3, 'sop_instance_uid')):
        if (2, tag) in meta and decode_uid(data, meta[(2, tag)]) != values[key]:
            raise CaptureError('file meta/image UID mismatch')
    return metadata, pixel
