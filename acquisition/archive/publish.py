import json
from acquisition.archive.schema import capture_identity, make_manifest, encode_manifest
from acquisition.receipt.identity import compare_identity
from acquisition.receipt.receipts import SourceReceipt, insert_receipt
from acquisition.frames.quality import check_capture


class CapturePublisher:
    def __init__(self, store):
        self.store = store

    def publish(self, snapshot, metadata, frames, report):
        checked = check_capture(snapshot, metadata, frames)
        if checked != report:
            from acquisition.receipt.source import CaptureError
            raise CaptureError('candidate changed after quality gate')
        with self.store.transaction() as connection:
            old = self.store.load_receipt(snapshot.source_id)
            body = None if old is None else self.store.load_source(old.attachment_id)['body']
            decision = compare_identity(snapshot, old, body)
            if decision.replay:
                from acquisition.archive.read import CaptureReader
                from acquisition.archive.schema import NotFound, IntegrityError
                try:
                    CaptureReader(self.store).read(old.capture_id)
                except NotFound as exc:
                    raise IntegrityError('receipt points to missing capture') from exc
                result = old
            else:
                capture_id = capture_identity(snapshot.source_id, snapshot.digest)
                attachment_id = capture_id + ':source'
                result = SourceReceipt(snapshot.source_id, snapshot.digest, attachment_id,
                                       capture_id, len(frames))
                manifest = make_manifest(snapshot, metadata, frames, result)
                connection.execute('INSERT INTO sources VALUES (?, ?, ?)',
                                   (attachment_id, snapshot.digest, snapshot.body))
                self.store.checkpoint('source')
                self.store.insert_capture(capture_id, snapshot.source_id, attachment_id,
                                          encode_manifest(manifest))
                self.store.checkpoint('capture')
                for frame in frames:
                    spans = json.dumps([[span.start, span.end] for span in frame.spans],
                                       separators=(',', ':'))
                    connection.execute('INSERT INTO frames VALUES (?, ?, ?, ?, ?)',
                                       (capture_id, frame.index, frame.body, frame.digest, spans))
                    self.store.checkpoint('frame:' + str(frame.index))
                insert_receipt(connection, result)
                self.store.checkpoint('receipt')
                self.store.checkpoint('before_commit')
        return result
