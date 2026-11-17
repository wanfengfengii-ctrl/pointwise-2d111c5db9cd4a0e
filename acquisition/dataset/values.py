import re
import struct
from acquisition.receipt.source import CaptureError

UID_PATTERN = re.compile(r'[0-9]+(?:\.[0-9]+)*')


def valid_uid(value):
    """Apply the archive's UI syntax contract to a decoded string."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    if UID_PATTERN.fullmatch(value) is None:
        return False
    return not any(len(part) > 1 and part[0] == '0' for part in value.split('.'))


def decode_text(data, element, vr):
    if element.vr != vr:
        raise CaptureError('wrong VR for required value')
    try:
        return data[element.value_start:element.end].decode('ascii')
    except UnicodeError as exc:
        raise CaptureError('required value is not ASCII') from exc


def decode_uid(data, element):
    text = decode_text(data, element, 'UI')
    text = text[:-1] if text.endswith('\0') else text
    if len(text) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', text):
        raise CaptureError('invalid UID')
    if any(len(part) > 1 and part[0] == '0' for part in text.split('.')):
        raise CaptureError('invalid UID component')
    return text


def decode_positive_is(data, element):
    text = decode_text(data, element, 'IS')
    value = text.strip(' ')
    if len(text) > 12 or not re.fullmatch(r'\+?[0-9]+', value) or int(value) <= 0:
        raise CaptureError('NumberOfFrames must be a complete positive IS')
    return int(value)


def decode_us(data, element):
    if element.vr != 'US' or element.length != 2:
        raise CaptureError('required US must have width two')
    return struct.unpack_from('<H', data, element.value_start)[0]
