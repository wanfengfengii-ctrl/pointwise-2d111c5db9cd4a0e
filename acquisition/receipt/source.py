from dataclasses import dataclass
from hashlib import sha256


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    body: bytes
    digest: str


def snapshot_source(source_id, file_bytes):
    if not isinstance(source_id, str) or not source_id:
        raise CaptureError('source_id must be a nonempty string')
    try:
        source_id.encode('utf-8')
    except UnicodeError as exc:
        raise CaptureError('source_id is not UTF-8 encodable') from exc
    if not isinstance(file_bytes, (bytes, bytearray, memoryview)):
        raise CaptureError('file_bytes must be bytes-like')
    try:
        body = bytes(file_bytes)
    except (ValueError, TypeError, BufferError) as exc:
        raise CaptureError('file_bytes cannot be snapshotted') from exc
    return SourceSnapshot(source_id, body, sha256(body).hexdigest())


def verify_source_digest(body, digest):
    if not isinstance(body, bytes) or sha256(body).hexdigest() != digest:
        raise CaptureError('SOURCE_DIGEST_MISMATCH')
