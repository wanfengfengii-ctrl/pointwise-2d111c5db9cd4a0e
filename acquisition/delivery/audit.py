from dataclasses import dataclass
from hashlib import sha256
import json
from acquisition.archive.schema import IntegrityError, decode_manifest
from acquisition.receipt.source import CaptureError, verify_source_digest
from acquisition.encapsulation.provenance import SourceSpan, rebuild_bytes


@dataclass(frozen=True)
class AuditFrameResult:
    index: int
    size: int
    digest: str
    span_count: int


class EvidenceAuditor:
    def __init__(self, store):
        self.store = store

    def audit(self, capture_id):
        record = self.store.load_capture(capture_id)
        try:
            manifest = decode_manifest(record['manifest'])
            if (manifest['capture_id'] != capture_id or record['source_id'] != manifest['source_id']
                    or record['attachment_id'] != manifest['attachment_id']
                    or record['receipt'] is None
                    or record['receipt'].as_dict() != manifest['receipt']):
                raise IntegrityError('audit relationship mismatch')
            source = self.store.load_source(record['attachment_id'])
            raw = source['body']
            verify_source_digest(raw, source['digest'])
            if source['digest'] != manifest['source_digest'] or len(raw) != manifest['source_size']:
                raise IntegrityError('audit source binding mismatch')
            if len(record['frames']) != len(manifest['frames']):
                raise IntegrityError('audit missing frame')
            results = []
            for entry, row in zip(manifest['frames'], record['frames']):
                index, body, digest, encoded = row
                pairs = json.loads(encoded)
                if index != entry['index'] or digest != entry['digest'] or pairs != entry['spans']:
                    raise IntegrityError('audit frame link mismatch')
                reconstructed = rebuild_bytes(raw, tuple(SourceSpan(*pair) for pair in pairs))
                if reconstructed != body or len(body) != entry['size']:
                    raise IntegrityError('FRAME_SOURCE_MISMATCH')
                actual = sha256(reconstructed).hexdigest()
                if actual != digest or sha256(body).hexdigest() != digest:
                    raise IntegrityError('audit digest mismatch')
                results.append(AuditFrameResult(index, len(body), actual, len(pairs)))
            return tuple(results)
        except IntegrityError:
            raise
        except (CaptureError, ValueError, TypeError, KeyError) as exc:
            raise IntegrityError('corrupt evidence: ' + str(exc)) from exc
