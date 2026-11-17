from dataclasses import dataclass
from acquisition.receipt.source import CaptureError, verify_source_digest


class SourceConflict(CaptureError):
    pass


@dataclass(frozen=True)
class IdentityDecision:
    replay: bool
    receipt: object = None


def compare_identity(snapshot, receipt, archived_body=None):
    if receipt is None:
        return IdentityDecision(False)
    from acquisition.archive.schema import IntegrityError
    try:
        verify_source_digest(archived_body, receipt.source_digest)
    except CaptureError as exc:
        raise IntegrityError(str(exc)) from exc
    if receipt.source_id != snapshot.source_id:
        raise IntegrityError('receipt identity mismatch')
    if receipt.source_digest != snapshot.digest or archived_body != snapshot.body:
        raise SourceConflict('source_id is already bound to different bytes')
    return IdentityDecision(True, receipt)
