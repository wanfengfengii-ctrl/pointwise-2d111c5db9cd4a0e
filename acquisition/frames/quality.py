from dataclasses import dataclass
from hashlib import sha256
from acquisition.receipt.source import CaptureError, verify_source_digest
from acquisition.encapsulation.provenance import rebuild_bytes


@dataclass(frozen=True)
class QualityReport:
    source_digest: str
    frame_digests: tuple
    frame_count: int


def frame_digest(body):
    return sha256(body).hexdigest()


def check_capture(snapshot, metadata, frames):
    verify_source_digest(snapshot.body, snapshot.digest)
    if len(frames) != metadata.frame_count:
        raise CaptureError('frame count mismatch at quality gate')
    digests = []
    previous_end = -1
    for index, frame in enumerate(frames):
        if frame.index != index or not frame.body or frame.boundary.start < previous_end:
            raise CaptureError('non-contiguous frame order')
        if rebuild_bytes(snapshot.body, frame.spans) != frame.body:
            raise CaptureError('FRAME_SOURCE_MISMATCH: evidence differs from body')
        digest = frame_digest(frame.body)
        if digest != frame.digest:
            raise CaptureError('frame digest mismatch')
        previous_end = frame.boundary.end
        digests.append(digest)
    return QualityReport(snapshot.digest, tuple(digests), len(frames))
