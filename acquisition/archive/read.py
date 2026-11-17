import json
from hashlib import sha256
from acquisition.archive.schema import (ArchivedFrame, IntegrityError, NotFound,
                                        decode_manifest)
from acquisition.receipt.source import CaptureError, verify_source_digest
from acquisition.encapsulation.provenance import SourceSpan, rebuild_bytes


class CaptureReader:
    def __init__(self, store):
        self.store = store

    def checked_capture(self, capture_id):
        record = self.store.load_capture(capture_id)
        try:
            manifest = decode_manifest(record['manifest'])
            receipt = record['receipt']
            if (record['source_id'] != manifest['source_id']
                    or record['attachment_id'] != manifest['attachment_id']
                    or capture_id != manifest['capture_id']
                    or receipt is None or receipt.as_dict() != manifest['receipt']):
                raise IntegrityError('archive relationship mismatch')
            source = self.store.load_source(record['attachment_id'])
            verify_source_digest(source['body'], source['digest'])
            if source['digest'] != manifest['source_digest'] or len(source['body']) != manifest['source_size']:
                raise IntegrityError('source binding mismatch')
            if len(record['frames']) != len(manifest['frames']):
                raise IntegrityError('stored frame count mismatch')
            frames = []
            for entry, row in zip(manifest['frames'], record['frames']):
                index, body, digest, encoded_spans = row
                pairs = json.loads(encoded_spans)
                if index != entry['index'] or digest != entry['digest'] or pairs != entry['spans']:
                    raise IntegrityError('stored frame link mismatch')
                if not isinstance(body, bytes) or len(body) != entry['size'] or sha256(body).hexdigest() != digest:
                    raise IntegrityError('FRAME_DIGEST_MISMATCH')
                spans = tuple(SourceSpan(*pair) for pair in pairs)
                if rebuild_bytes(source['body'], spans) != body:
                    raise IntegrityError('FRAME_SOURCE_MISMATCH')
                frames.append(ArchivedFrame(index, body, digest, spans))
            return manifest, tuple(frames)
        except IntegrityError:
            raise
        except (CaptureError, ValueError, TypeError, KeyError) as exc:
            raise IntegrityError('corrupt fixed capture: ' + str(exc)) from exc

    def read(self, capture_id, frame_index=None):
        manifest, frames = self.checked_capture(capture_id)
        if frame_index is None:
            return manifest
        if type(frame_index) is not int or not 0 <= frame_index < len(frames):
            raise NotFound((capture_id, frame_index))
        return frames[frame_index]

    def read_derived(self, package_id):
        from acquisition.archive.derived import read_derived
        return read_derived(self.store, package_id)
