from contextlib import contextmanager
import sqlite3
from acquisition.archive.schema import SCHEMA_VERSION, IntegrityError, NotFound
from acquisition.receipt.receipts import lookup_receipt

DDL = (
    'CREATE TABLE archive_info (version INTEGER NOT NULL)',
    'CREATE TABLE sources (attachment_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body BLOB NOT NULL)',
    'CREATE TABLE captures (capture_id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL, '
    'attachment_id TEXT UNIQUE NOT NULL REFERENCES sources(attachment_id), manifest TEXT NOT NULL)',
    'CREATE TABLE frames (capture_id TEXT NOT NULL REFERENCES captures(capture_id), '
    'frame_index INTEGER NOT NULL, body BLOB NOT NULL, digest TEXT NOT NULL, spans TEXT NOT NULL, '
    'PRIMARY KEY(capture_id, frame_index))',
    'CREATE TABLE receipts (source_id TEXT PRIMARY KEY, source_digest TEXT NOT NULL, '
    'attachment_id TEXT UNIQUE NOT NULL REFERENCES sources(attachment_id), '
    'capture_id TEXT UNIQUE NOT NULL REFERENCES captures(capture_id), frame_count INTEGER NOT NULL)'
)
COLUMNS = {
    'archive_info': ('version',),
    'sources': ('attachment_id', 'digest', 'body'),
    'captures': ('capture_id', 'source_id', 'attachment_id', 'manifest'),
    'frames': ('capture_id', 'frame_index', 'body', 'digest', 'spans'),
    'receipts': ('source_id', 'source_digest', 'attachment_id', 'capture_id', 'frame_count')
}


class ArchiveStore:
    def __init__(self, path, fault=None):
        self.path = str(path)
        self.fault = fault
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.execute('PRAGMA foreign_keys = ON')
        try:
            self.initialize()
        except BaseException:
            self.connection.close()
            raise

    def initialize(self):
        with self.transaction():
            tables = {row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                for statement in DDL:
                    self.connection.execute(statement)
                self.connection.execute('INSERT INTO archive_info VALUES (?)', (SCHEMA_VERSION,))
            elif tables != set(COLUMNS):
                raise IntegrityError('unsupported archive structure')
            for statement in DDL:
                table = statement.split()[2]
                actual_sql = self.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                if actual_sql != statement:
                    raise IntegrityError('unsupported archive table definition: ' + table)
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
                raise IntegrityError('archive triggers are unsupported')
            for table, columns in COLUMNS.items():
                actual = tuple(row[1] for row in self.connection.execute('PRAGMA table_info(' + table + ')'))
                if actual != columns:
                    raise IntegrityError('unsupported archive columns: ' + table)
            versions = self.connection.execute('SELECT version FROM archive_info').fetchall()
            if versions != [(SCHEMA_VERSION,)]:
                raise IntegrityError('unsupported archive schema version')
            expected_foreign = {'captures': 1, 'frames': 1, 'receipts': 2}
            for table, count in expected_foreign.items():
                if len(self.connection.execute('PRAGMA foreign_key_list(' + table + ')').fetchall()) != count:
                    raise IntegrityError('missing archive foreign keys')

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def checkpoint(self, stage):
        if self.fault is not None:
            self.fault(stage)

    def load_receipt(self, source_id):
        return lookup_receipt(self.connection, source_id)

    def load_source(self, attachment_id):
        row = self.connection.execute('SELECT digest, body FROM sources WHERE attachment_id=?',
                                      (attachment_id,)).fetchone()
        if row is None:
            raise IntegrityError('missing source attachment')
        return {'attachment_id': attachment_id, 'digest': row[0], 'body': row[1]}

    def load_capture(self, capture_id):
        row = self.connection.execute(
            'SELECT source_id, attachment_id, manifest FROM captures WHERE capture_id=?',
            (capture_id,)).fetchone()
        if row is None:
            raise NotFound(capture_id)
        frames = self.connection.execute(
            'SELECT frame_index, body, digest, spans FROM frames WHERE capture_id=? ORDER BY frame_index',
            (capture_id,)).fetchall()
        return {'capture_id': capture_id, 'source_id': row[0], 'attachment_id': row[1],
                'manifest': row[2], 'frames': frames, 'receipt': self.load_receipt(row[0])}

    def insert_capture(self, capture_id, source_id, attachment_id, manifest):
        self.connection.execute('INSERT INTO captures VALUES (?, ?, ?, ?)',
                                (capture_id, source_id, attachment_id, manifest))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
