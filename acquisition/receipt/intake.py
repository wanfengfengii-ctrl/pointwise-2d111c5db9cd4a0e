from dataclasses import dataclass
from acquisition.receipt.source import snapshot_source
from acquisition.receipt.identity import compare_identity


@dataclass(frozen=True)
class PreparedSource:
    snapshot: object
    replay_receipt: object = None


def prepare_source(store, source_id, file_bytes):
    snapshot = snapshot_source(source_id, file_bytes)
    receipt = store.load_receipt(source_id)
    body = None if receipt is None else store.load_source(receipt.attachment_id)['body']
    decision = compare_identity(snapshot, receipt, body)
    if decision.replay:
        from acquisition.archive.read import CaptureReader
        from acquisition.archive.schema import NotFound, IntegrityError
        try:
            CaptureReader(store).read(receipt.capture_id)
        except NotFound as exc:
            raise IntegrityError('receipt points to missing capture') from exc
    return PreparedSource(snapshot, decision.receipt)
