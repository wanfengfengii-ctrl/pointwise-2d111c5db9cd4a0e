from dataclasses import dataclass
from acquisition.receipt.source import CaptureError
from acquisition.receipt.intake import prepare_source
from acquisition.dataset.metadata import parse_metadata
from acquisition.encapsulation.items import read_items
from acquisition.encapsulation.offsets import parse_offset_table
from acquisition.encapsulation.payload import build_payload_view
from acquisition.frames.extract import extract_frames
from acquisition.frames.quality import check_capture
from acquisition.archive.publish import CapturePublisher


@dataclass(frozen=True)
class BatchItemResult:
    source_id: str
    receipt: object = None
    error_type: str = ''
    error: str = ''


class CaptureService:
    def __init__(self, store):
        self.store = store

    def ingest(self, source_id, file_bytes):
        prepared = prepare_source(self.store, source_id, file_bytes)
        if prepared.replay_receipt is not None:
            return prepared.replay_receipt
        snapshot = prepared.snapshot
        metadata, location = parse_metadata(snapshot.body)
        directory = read_items(snapshot.body, location)
        offsets = parse_offset_table(directory, metadata.frame_count)
        view = build_payload_view(snapshot.body, directory)
        frames = extract_frames(view, metadata, offsets)
        report = check_capture(snapshot, metadata, frames)
        return CapturePublisher(self.store).publish(snapshot, metadata, frames, report)

    def ingest_batch(self, entries):
        result = []
        for source_id, body in entries:
            try:
                receipt = self.ingest(source_id, body)
                result.append(BatchItemResult(source_id, receipt))
            except CaptureError as exc:
                result.append(BatchItemResult(source_id, error_type=type(exc).__name__, error=str(exc)))
        return tuple(result)

    def derive(self, package_id, capture_ids, uid_map):
        from acquisition.archive.derived import publish_derived
        return publish_derived(self.store, package_id, capture_ids, uid_map)
