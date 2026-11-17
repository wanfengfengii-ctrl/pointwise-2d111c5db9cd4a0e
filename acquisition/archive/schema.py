from dataclasses import dataclass
import hashlib
import json
from acquisition.receipt.source import CaptureError
from acquisition.dataset.metadata import validate_metadata
from acquisition.dataset.values import valid_uid

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


def _valid_span_list(pairs, name, size_total):
    if not isinstance(pairs, list) or not pairs:
        raise ValueError(name + ' must be a nonempty list')
    previous = -1
    total = 0
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(name + ' representation')
        start, end = pair
        if type(start) is not int or type(end) is not int:
            raise ValueError(name + ' coordinates')
        if not 0 <= start < end or start < previous:
            raise ValueError(name + ' bounds/order')
        previous = end
        total += end - start
    if total != size_total:
        raise ValueError(name + ' size')
    return total


def validate_derived_manifest(value):
    try:
        required = {'version', 'package_id', 'capture_ids', 'uid_map', 'files'}
        if (not isinstance(value, dict) or set(value) != required
                or type(value['version']) is not int or value['version'] != 1):
            raise ValueError('derived manifest fields/version')
        package_id = value['package_id']
        if not isinstance(package_id, str) or not package_id:
            raise ValueError('package identity')
        package_id.encode('utf-8')
        captures = value['capture_ids']
        if (not isinstance(captures, list) or not captures
                or not all(isinstance(item, str) and item for item in captures)
                or len(set(captures)) != len(captures)):
            raise ValueError('parent capture list')
        mapping = value['uid_map']
        if not isinstance(mapping, dict):
            raise ValueError('uid_map representation')
        for old, new in mapping.items():
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError('uid_map strings')
            if not valid_uid(old) or not valid_uid(new):
                raise ValueError('uid_map syntax')
        if len(set(mapping.values())) != len(mapping):
            raise ValueError('uid_map values must be distinct')
        files = value['files']
        if not isinstance(files, list) or len(files) != len(captures):
            raise ValueError('derived file count')
        for ordinal, entry in enumerate(files):
            if not isinstance(entry, dict) or set(entry) != {
                    'parent_capture_id', 'source_digest', 'derived_digest', 'frames'}:
                raise ValueError('derived file fields')
            if entry['parent_capture_id'] != captures[ordinal]:
                raise ValueError('parent binding')
            if not valid_digest(entry['source_digest']) or not valid_digest(entry['derived_digest']):
                raise ValueError('derived digest encoding')
            frames = entry['frames']
            if not isinstance(frames, list) or not frames:
                raise ValueError('derived frame list')
            for index, frame in enumerate(frames):
                if not isinstance(frame, dict) or set(frame) != {
                        'index', 'size', 'digest', 'source_spans', 'derived_spans'}:
                    raise ValueError('derived frame fields')
                if type(frame['index']) is not int or frame['index'] != index:
                    raise ValueError('derived frame index')
                if type(frame['size']) is not int or frame['size'] <= 0 or not valid_digest(frame['digest']):
                    raise ValueError('derived frame size/digest')
                _valid_span_list(frame['source_spans'], 'source spans', frame['size'])
                _valid_span_list(frame['derived_spans'], 'derived spans', frame['size'])
        return value
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise IntegrityError('invalid derived manifest: ' + str(exc)) from exc


def encode_derived_manifest(value):
    validate_derived_manifest(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def decode_derived_manifest(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return validate_derived_manifest(json.loads(text, object_pairs_hook=unique))
    except (ValueError, TypeError) as exc:
        raise IntegrityError('invalid derived manifest JSON') from exc
