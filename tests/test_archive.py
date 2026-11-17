from pathlib import Path
import sqlite3
import tempfile
import unittest
from acquisition.archive.store import ArchiveStore
from acquisition.archive.read import CaptureReader
from acquisition.archive.schema import IntegrityError, NotFound
from acquisition.delivery.batch import CaptureService
from tests.fixtures import make_case


class TestArchive(unittest.TestCase):
    def test_publish_rollback(self):
        case = make_case()
        for stage in ('source', 'capture', 'frame:0', 'frame:1', 'receipt', 'before_commit'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'archive.sqlite'
                with ArchiveStore(path) as store:
                    old = CaptureService(store).ingest('old', case.body)
                    baseline = CaptureReader(store).read(old.capture_id)
                    store.initialize()
                    store.initialize()
                    def fail(current):
                        if current == stage:
                            raise OSError('injected publication failure at ' + stage)
                    store.fault = fail
                    with self.assertRaisesRegex(OSError, 'injected publication'):
                        CaptureService(store).ingest('new', case.body)
                with ArchiveStore(path) as reopened:
                    self.assertIsNone(reopened.load_receipt('new'))
                    self.assertEqual(CaptureReader(reopened).read(old.capture_id), baseline)
                    for table in ('sources', 'captures', 'receipts'):
                        self.assertEqual(reopened.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
                    self.assertEqual(reopened.connection.execute('SELECT count(*) FROM frames').fetchone()[0], 2)
                    result = CaptureService(reopened).ingest('new', case.body)
                    self.assertEqual(CaptureService(reopened).ingest('new', case.body), result)
                    self.assertEqual(CaptureReader(reopened).read(result.capture_id, 1).body, case.frames[1])
                    self.assertEqual(reopened.connection.execute('SELECT count(*) FROM captures').fetchone()[0], 2)

    def test_initialize_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite'
            case = make_case()
            with ArchiveStore(path) as store:
                result = CaptureService(store).ingest('fixed', case.body)
                before = list(store.connection.iterdump())
                store.initialize()
                self.assertEqual(list(store.connection.iterdump()), before)
                with self.assertRaises(NotFound):
                    CaptureReader(store).read('absent')
                for index in (-1, 2, '0', True):
                    with self.assertRaises(NotFound):
                        CaptureReader(store).read(result.capture_id, index)
                store.connection.execute('UPDATE archive_info SET version=2')
            with self.assertRaises(IntegrityError):
                ArchiveStore(path)
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute('SELECT count(*) FROM captures').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT version FROM archive_info').fetchall(), [(2,)])
            connection.close()
            other = Path(directory) / 'unknown.sqlite'
            connection = sqlite3.connect(other)
            connection.execute('CREATE TABLE unrelated (value TEXT)')
            connection.execute("INSERT INTO unrelated VALUES ('preserve')")
            connection.commit()
            connection.close()
            with self.assertRaises(IntegrityError):
                ArchiveStore(other)
            connection = sqlite3.connect(other)
            self.assertEqual(connection.execute('SELECT value FROM unrelated').fetchall(), [('preserve',)])
            connection.close()
