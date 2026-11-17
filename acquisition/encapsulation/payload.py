from dataclasses import dataclass
from acquisition.receipt.source import CaptureError


@dataclass(frozen=True)
class PayloadSegment:
    logical_start: int
    logical_end: int
    source_start: int
    item_index: int


@dataclass(frozen=True)
class PayloadView:
    body: bytes
    segments: tuple

    def read(self, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(self.body):
            raise CaptureError('payload range outside logical view')
        return self.body[start:end]

    def is_item_end(self, position):
        return any(segment.logical_end == position for segment in self.segments)


def build_payload_view(data, directory):
    chunks = []
    segments = []
    logical = 0
    last_end = -1
    for fragment in directory.fragments:
        if fragment.payload_start < last_end or fragment.payload_end > len(data):
            raise CaptureError('unordered or out-of-range fragment')
        chunks.append(data[fragment.payload_start:fragment.payload_end])
        segments.append(PayloadSegment(logical, logical + fragment.length,
                                       fragment.payload_start, fragment.index))
        logical += fragment.length
        last_end = fragment.payload_end
    return PayloadView(b''.join(chunks), tuple(segments))
