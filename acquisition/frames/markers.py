from dataclasses import dataclass
import struct
from acquisition.receipt.source import CaptureError


@dataclass(frozen=True)
class Marker:
    code: int
    start: int
    end: int


def read_marker(data, position):
    start = position
    if position >= len(data) or data[position] != 0xff:
        raise CaptureError('JPEG marker prefix required')
    while position < len(data) and data[position] == 0xff:
        position += 1
    if position == len(data) or data[position] == 0:
        raise CaptureError('truncated or escaped marker outside scan')
    return Marker(data[position], start, position + 1)


def read_segment(data, marker):
    if marker.code in {0xd8, 0xd9, 0x01} or 0xd0 <= marker.code <= 0xd7:
        raise CaptureError('standalone marker has no segment')
    if marker.end + 2 > len(data):
        raise CaptureError('missing segment length')
    length = struct.unpack_from('>H', data, marker.end)[0]
    end = marker.end + length
    if length < 2 or end > len(data):
        raise CaptureError('JPEG segment length outside payload')
    return data[marker.end + 2:end], end


def validate_baseline_header(payload, metadata):
    if len(payload) < 6:
        raise CaptureError('short SOF0')
    precision, height, width, count = struct.unpack_from('>BHHB', payload)
    if (precision, height, width, count) != (
            8, metadata.rows, metadata.columns, metadata.samples_per_pixel):
        raise CaptureError('SOF0 disagrees with image metadata')
    if len(payload) != 6 + 3 * count:
        raise CaptureError('SOF0 component length')
    components = {}
    for i in range(count):
        identity, sampling, table = payload[6 + i * 3:9 + i * 3]
        if identity in components or not 1 <= sampling >> 4 <= 4 or not 1 <= sampling & 15 <= 4 or table > 3:
            raise CaptureError('invalid SOF0 component')
        components[identity] = table
    return components


def parse_quantization(payload):
    tables = set()
    position = 0
    while position < len(payload):
        selector = payload[position]
        if selector >> 4 != 0 or selector & 15 > 3 or position + 65 > len(payload):
            raise CaptureError('Baseline DQT must contain 8-bit tables')
        if 0 in payload[position + 1:position + 65]:
            raise CaptureError('zero quantization coefficient')
        tables.add(selector & 15)
        position += 65
    if not tables:
        raise CaptureError('empty DQT')
    return tables


def parse_huffman(payload):
    tables = set()
    position = 0
    while position < len(payload):
        if position + 17 > len(payload):
            raise CaptureError('short DHT')
        selector = payload[position]
        counts = payload[position + 1:position + 17]
        size = sum(counts)
        space = 1
        for count in counts:
            space = space * 2 - count
            if space < 0:
                raise CaptureError('oversubscribed Huffman table')
        if selector >> 4 > 1 or selector & 15 > 3 or not size or size > 256 or space == 0:
            raise CaptureError('invalid Baseline Huffman table')
        end = position + 17 + size
        if end > len(payload):
            raise CaptureError('truncated Huffman symbols')
        symbols = payload[position + 17:end]
        if len(set(symbols)) != len(symbols):
            raise CaptureError('duplicate Huffman symbol')
        if selector >> 4 == 0 and any(symbol > 11 for symbol in symbols):
            raise CaptureError('invalid DC category')
        if selector >> 4 == 1 and any(
                (symbol & 15) > 10 or ((symbol & 15) == 0 and symbol not in {0, 0xf0})
                for symbol in symbols):
            raise CaptureError('invalid AC symbol')
        tables.add((selector >> 4, selector & 15))
        position = end
    if not tables:
        raise CaptureError('empty DHT')
    return tables
