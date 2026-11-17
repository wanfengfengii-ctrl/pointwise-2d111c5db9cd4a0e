"""Persistence and verification for fixed derived research packages.

The package is fully determined by the ordered parent captures and the UID map.
Nothing here consults an external original file: all expected bytes are
recomputed from the archived parent attachments and compared with the archived
copy.
"""
from hashlib import sha256

from acquisition.archive.schema import (IntegrityError, NotFound,
                                        decode_derived_manifest,
                                        encode_derived_manifest)
from acquisition.dataset.metadata import parse_metadata
from acquisition.dataset.rewrite import rewrite_file, validate_uid_map
from acquisition.encapsulation.provenance import SourceSpan, rebuild_bytes
from acquisition.receipt.source import CaptureError


def validate_package_inputs(package_id, capture_ids, uid_map):
    if not isinstance(package_id, str) or not package_id:
        raise CaptureError('package_id must be a nonempty string')
    try:
        package_id.encode('utf-8')
    except UnicodeError as exc:
        raise CaptureError('package_id is not UTF-8 encodable') from exc
    if not isinstance(capture_ids, (list, tuple)) or not capture_ids:
        raise CaptureError('capture_ids must be a nonempty list or tuple')
    for capture_id in capture_ids:
        if not isinstance(capture_id, str) or not capture_id:
            raise CaptureError('capture_ids must be nonempty strings')
    if len(set(capture_ids)) != len(capture_ids):
        raise CaptureError('capture_ids must not repeat')
    return list(capture_ids), validate_uid_map(uid_map)


def _frames_of(body):
    """Re-run the acquisition chain over one Part 10 body (derive/audit only)."""
    from acquisition.encapsulation.items import read_items
    from acquisition.encapsulation.offsets import parse_offset_table
    from acquisition.encapsulation.payload import build_payload_view
    from acquisition.frames.extract import extract_frames
    metadata, location = parse_metadata(body)
    directory = read_items(body, location)
    offsets = parse_offset_table(directory, metadata.frame_count)
    view = build_payload_view(body, directory)
    return extract_frames(view, metadata, offsets)


def _source_file(store, parent_capture_id):
    from acquisition.archive.read import CaptureReader
    try:
        return CaptureReader(store).checked_capture(parent_capture_id)
    except NotFound as exc:
        raise IntegrityError('package parent capture is missing') from exc


def build_package(store, capture_ids, mapping):
    """Plan the fixed result; no writes and no package identity consumed."""
    files = []
    seen = set()
    for capture_id in capture_ids:
        parent_manifest, parent_frames = _source_file(store, capture_id)
        source_body = store.load_source(parent_manifest['attachment_id'])['body']
        derived_body, found = rewrite_file(source_body, mapping)
        seen |= found
        # The copy must remain fully interpretable by the acquisition chain.
        derived_frames = _frames_of(derived_body)
        if len(derived_frames) != len(parent_frames):
            raise CaptureError('derived frame count differs from parent')
        entries = []
        for frame, parent_frame in zip(derived_frames, parent_frames):
            if frame.index != parent_frame.index:
                raise CaptureError('derived frame order differs from parent')
            if frame.body != parent_frame.body or frame.digest != parent_frame.digest:
                raise CaptureError('derived frame bytes differ from parent')
            source_spans = [[span.start, span.end] for span in parent_frame.spans]
            derived_spans = [[span.start, span.end] for span in frame.spans]
            if rebuild_bytes(source_body, parent_frame.spans) != rebuild_bytes(
                    derived_body, frame.spans):
                raise CaptureError('frame provenance rebuild mismatch')
            entries.append({'index': frame.index, 'size': len(frame.body),
                            'digest': frame.digest,
                            'source_spans': source_spans,
                            'derived_spans': derived_spans})
        files.append({'parent_capture_id': capture_id,
                      'source_digest': parent_manifest['source_digest'],
                      'derived_digest': sha256(derived_body).hexdigest(),
                      'frames': entries,
                      '_body': derived_body})
    if seen != set(mapping):
        raise CaptureError('uid_map keys must exactly cover parent target UIDs')
    bodies = [entry.pop('_body') for entry in files]
    value = {'version': 1, 'package_id': None,
             'capture_ids': list(capture_ids), 'uid_map': dict(mapping),
             'files': files}
    return value, bodies


def publish_derived(store, package_id, capture_ids, uid_map):
    ids, mapping = validate_package_inputs(package_id, capture_ids, uid_map)
    for capture_id in ids:
        # Missing parents are input errors; corrupt ones surface as IntegrityError
        # through the parent reader below.
        try:
            store.load_capture(capture_id)
        except NotFound as exc:
            raise CaptureError('capture_ids must name existing captures') from exc
    existing = store.load_derived(package_id)
    if existing is not None:
        # Verify the archived package before deciding replay/conflict, so damage
        # is never mistaken for an ordinary replay or silently repaired.
        read_derived(store, package_id)
        manifest = decode_derived_manifest(existing['manifest'])
        if manifest['capture_ids'] != ids or manifest['uid_map'] != mapping:
            from acquisition.receipt.identity import SourceConflict
            raise SourceConflict('package_id is bound to different parents or mapping')
        return package_id
    value, bodies = build_package(store, ids, mapping)
    value['package_id'] = package_id
    encoded = encode_derived_manifest(value)
    with store.transaction():
        store.insert_derived(package_id, encoded, bodies)
        for index in range(len(bodies)):
            store.checkpoint('derived_file:' + str(index))
        store.checkpoint('derived_manifest')
        store.checkpoint('before_commit')
    return package_id


def load_verified_package(store, package_id, deep=False):
    """Return (manifest value with bodies, derived bodies) after full checks.

    ``deep=False`` verifies everything that can be decided from stored records
    and archived bytes: digests, parent binding, the fixed rewrite recomputed
    from each archived parent, and both sides' stored frame provenance rebuilt
    into identical JPEG bodies.  It touches no parser in ``acquisition.frames``,
    so fixed reads and exports work in a process that forbids extraction.

    ``deep=True`` additionally re-runs the acquisition chain over the archived
    parent and copy bytes and requires the freshly extracted frame boundaries,
    bodies and digests to match the stored provenance.

    Corrupt archived bytes, relationships, mappings or provenance surface as
    IntegrityError; only a genuinely absent package is NotFound.
    """
    try:
        return _load_verified_package(store, package_id, deep)
    except (IntegrityError, NotFound):
        raise
    except (CaptureError, ValueError, TypeError, KeyError) as exc:
        raise IntegrityError('corrupt derived package: ' + str(exc)) from exc


def _load_verified_package(store, package_id, deep):
    record = store.load_derived(package_id)
    if record is None:
        raise NotFound(package_id)
    manifest = decode_derived_manifest(record['manifest'])
    if manifest['package_id'] != package_id:
        raise IntegrityError('derived package identity mismatch')
    rows = record['files']
    if len(rows) != len(manifest['files']) or [row[0] for row in rows] != list(range(len(rows))):
        raise IntegrityError('stored derived file count mismatch')
    bodies = []
    seen = set()
    for entry, row in zip(manifest['files'], rows):
        body = row[1]
        if not isinstance(body, bytes) or sha256(body).hexdigest() != entry['derived_digest']:
            raise IntegrityError('DERIVED_DIGEST_MISMATCH')
        parent_manifest, parent_frames = _source_file(store, entry['parent_capture_id'])
        if parent_manifest['source_digest'] != entry['source_digest']:
            raise IntegrityError('parent source binding mismatch')
        source_body = store.load_source(parent_manifest['attachment_id'])['body']
        # Recompute the expected copy from the archived parent and stored map.
        expected, found = rewrite_file(source_body, manifest['uid_map'])
        seen |= found
        if expected != body:
            raise IntegrityError('derived body is not the fixed rewrite')
        if len(parent_frames) != len(entry['frames']):
            raise IntegrityError('derived frame count differs from parent')
        if deep:
            parent_frames = _frames_of(source_body)
            derived_frames = _frames_of(body)
            if len(derived_frames) != len(entry['frames']):
                raise IntegrityError('derived frame count differs from manifest')
        for ordinal, frame_entry in enumerate(entry['frames']):
            if frame_entry['index'] != ordinal:
                raise IntegrityError('derived frame index binding mismatch')
            parent_frame = parent_frames[ordinal]
            parent_spans = [[span.start, span.end] for span in parent_frame.spans]
            if frame_entry['source_spans'] != parent_spans:
                raise IntegrityError('source frame provenance mismatch')
            source_bytes = rebuild_bytes(source_body, parent_frame.spans)
            derived_bytes = rebuild_bytes(
                body, [SourceSpan(pair[0], pair[1]) for pair in frame_entry['derived_spans']])
            if source_bytes != derived_bytes:
                raise IntegrityError('FRAME_SOURCE_MISMATCH')
            if (sha256(source_bytes).hexdigest() != frame_entry['digest']
                    or len(source_bytes) != frame_entry['size']):
                raise IntegrityError('derived frame digest mismatch')
            if deep and (derived_frames[ordinal].body != source_bytes
                         or [[span.start, span.end] for span in derived_frames[ordinal].spans]
                         != frame_entry['derived_spans']
                         or [[span.start, span.end] for span in parent_frames[ordinal].spans]
                         != frame_entry['source_spans']):
                raise IntegrityError('derived derivation chain mismatch')
        bodies.append(body)
    if seen != set(manifest['uid_map']):
        raise IntegrityError('uid_map does not exactly cover parent target UIDs')
    value = {key: manifest[key] for key in ('package_id', 'capture_ids', 'uid_map', 'files')}
    value['files'] = [dict(entry, body=body) for entry, body in zip(value['files'], bodies)]
    return value, bodies


def read_derived(store, package_id):
    value, _ = load_verified_package(store, package_id)
    return value
