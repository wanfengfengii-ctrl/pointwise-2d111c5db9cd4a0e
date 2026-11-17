from dataclasses import dataclass
import hashlib
import json
from acquisition.receipt.source import CaptureError
from acquisition.dataset.metadata import validate_metadata
from acquisition.encapsulation.provenance import SourceSpan

SCHEMA_VERSION = 1


class NotFound(LookupError):
    pass


class IntegrityError(CaptureError):
    pass


@dataclass(frozen=True)
class ArchiveManifest:
    value: dict


@dataclass(frozen=True)
class ArchivedFrame:
    index: int
    body: bytes
    digest: str
    spans: tuple


def capture_identity(source_id, source_digest):
    encoded = source_id.encode('utf-8')
    return hashlib.sha256(len(encoded).to_bytes(8, 'big') + encoded
                          + bytes.fromhex(source_digest)).hexdigest()


def valid_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def make_manifest(snapshot, metadata, frames, receipt):
    value = {
        'version': 1, 'capture_id': receipt.capture_id,
        'source_id': snapshot.source_id, 'source_digest': snapshot.digest,
        'source_size': len(snapshot.body), 'attachment_id': receipt.attachment_id,
        'metadata': metadata.as_dict(), 'receipt': receipt.as_dict(),
        'frames': [{'index': frame.index, 'size': len(frame.body),
                    'digest': frame.digest,
                    'spans': [[span.start, span.end] for span in frame.spans]}
                   for frame in frames]}
    validate_manifest(value)
    return ArchiveManifest(value)


def validate_manifest(value):
    try:
        required = {'version', 'capture_id', 'source_id', 'source_digest', 'source_size',
                    'attachment_id', 'metadata', 'receipt', 'frames'}
        if not isinstance(value, dict) or set(value) != required or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('manifest fields/version')
        if not isinstance(value['source_id'], str) or not value['source_id']:
            raise ValueError('source identity')
        if not valid_digest(value['source_digest']) or not valid_digest(value['capture_id']):
            raise ValueError('digest encoding')
        if capture_identity(value['source_id'], value['source_digest']) != value['capture_id']:
            raise ValueError('capture identity')
        if value['attachment_id'] != value['capture_id'] + ':source':
            raise ValueError('attachment binding')
        if type(value['source_size']) is not int or value['source_size'] <= 0:
            raise ValueError('source size')
        validate_metadata(value['metadata'])
        frames = value['frames']
        if not isinstance(frames, list) or len(frames) != value['metadata']['frame_count']:
            raise ValueError('manifest frame count')
        receipt = {'source_id': value['source_id'], 'source_digest': value['source_digest'],
                   'attachment_id': value['attachment_id'], 'capture_id': value['capture_id'],
                   'frame_count': len(frames)}
        if value['receipt'] != receipt:
            raise ValueError('receipt binding')
        previous = -1
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict) or set(frame) != {'index', 'size', 'digest', 'spans'}:
                raise ValueError('frame fields')
            if type(frame['index']) is not int or frame['index'] != index:
                raise ValueError('frame index')
            if type(frame['size']) is not int or frame['size'] <= 0 or not valid_digest(frame['digest']):
                raise ValueError('frame size/digest')
            if not isinstance(frame['spans'], list) or not frame['spans']:
                raise ValueError('frame spans')
            size = 0
            for pair in frame['spans']:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError('source span representation')
                start, end = pair
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= value['source_size'] or start < previous:
                    raise ValueError('source span bounds/order')
                previous = end
                size += end - start
            if size != frame['size']:
                raise ValueError('span size')
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise IntegrityError('invalid archive manifest: ' + str(exc)) from exc
    return value


def encode_manifest(manifest):
    value = manifest.value if isinstance(manifest, ArchiveManifest) else manifest
    validate_manifest(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def decode_manifest(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return validate_manifest(json.loads(text, object_pairs_hook=unique))
    except (ValueError, TypeError) as exc:
        raise IntegrityError('invalid manifest JSON') from exc
