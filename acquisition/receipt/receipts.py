from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceReceipt:
    source_id: str
    source_digest: str
    attachment_id: str
    capture_id: str
    frame_count: int

    def as_dict(self):
        return asdict(self)


def lookup_receipt(connection, source_id):
    row = connection.execute(
        'SELECT source_id, source_digest, attachment_id, capture_id, frame_count '
        'FROM receipts WHERE source_id = ?', (source_id,)).fetchone()
    return None if row is None else SourceReceipt(*row)


def insert_receipt(connection, receipt):
    connection.execute(
        'INSERT INTO receipts VALUES (?, ?, ?, ?, ?)',
        (receipt.source_id, receipt.source_digest, receipt.attachment_id,
         receipt.capture_id, receipt.frame_count))
