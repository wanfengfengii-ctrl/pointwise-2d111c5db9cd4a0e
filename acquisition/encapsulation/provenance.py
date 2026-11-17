from dataclasses import dataclass
from acquisition.receipt.source import CaptureError


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int


def project_range(view, start, end):
    view.read(start, end)
    if start == end:
        raise CaptureError('empty frame range')
    spans = []
    consumed = 0
    for segment in view.segments:
        left = max(start, segment.logical_start)
        right = min(end, segment.logical_end)
        if left < right:
            source_start = segment.source_start + left - segment.logical_start
            spans.append(SourceSpan(source_start, source_start + right - left))
            consumed += right - left
    if consumed != end - start:
        raise CaptureError('FRAME_SOURCE_MISMATCH: logical mapping gap')
    return tuple(spans)


def validate_spans(spans, source_length):
    previous = -1
    if not spans:
        raise CaptureError('empty provenance')
    for span in spans:
        if (type(span.start) is not int or type(span.end) is not int
                or not 0 <= span.start < span.end <= source_length or span.start < previous):
            raise CaptureError('invalid or unordered source interval')
        previous = span.end


def rebuild_bytes(source, spans):
    validate_spans(spans, len(source))
    return b''.join(source[span.start:span.end] for span in spans)
