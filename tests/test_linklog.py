import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    now = 5000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b


def load_linklog(directory):
    time_stub = types.SimpleNamespace(ticks_ms=_Clock.ticks_ms,
                                      ticks_diff=_Clock.ticks_diff)
    spec = importlib.util.spec_from_file_location(
        "linklog_test", ROOT / "software" / "linklog.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"time": time_stub}):
        spec.loader.exec_module(module)
    module.LOG_PATH = str(Path(directory) / "linklog.txt")
    module.OLD_PATH = str(Path(directory) / "linklog.old.txt")
    module.print = lambda *args, **kwargs: None
    return module


class LinkLogTests(unittest.TestCase):
    def setUp(self):
        _Clock.now = 5000
        self.dir = tempfile.TemporaryDirectory()
        self.log = load_linklog(self.dir.name)
        self.path = Path(self.log.LOG_PATH)
        self.old = Path(self.log.OLD_PATH)

    def tearDown(self):
        self.dir.cleanup()

    def test_lines_are_timestamped_and_bytes_are_readable(self):
        _Clock.now = 12345
        self.log.log("ttt", "rx", b"HFFFF00000001,1,0", 7)
        self.log.log("ttt", "bad", b"\xff\xfe")
        self.assertEqual(self.log.lines[0], "12.345 ttt rx HFFFF00000001,1,0 7")
        self.assertEqual(self.log.lines[1], "12.345 ttt bad b'\\xff\\xfe'")
        self.assertEqual(self.log.revision, 2)

    def test_ring_buffer_keeps_only_the_newest_lines(self):
        for i in range(self.log.RING_LINES + 5):
            self.log.log("n", i)
        self.assertEqual(len(self.log.lines), self.log.RING_LINES)
        self.assertTrue(self.log.lines[0].endswith(" n 5"))
        self.assertTrue(self.log.lines[-1].endswith(
            " n %d" % (self.log.RING_LINES + 4)))

    def test_writes_are_batched_until_enough_lines_or_time(self):
        self.log.log("n", "first")
        self.assertFalse(self.path.exists())
        for i in range(self.log.FLUSH_LINES - 1):
            self.log.log("n", i)
        text = self.path.read_text()
        self.assertTrue(text.startswith("--- session start, id ? ---\n"))
        self.assertIn(" n first\n", text)
        self.log.log("n", "later")
        self.assertNotIn("later", self.path.read_text())
        _Clock.now += self.log.FLUSH_MS
        self.log.log("n", "timed")
        self.assertIn(" n later\n", self.path.read_text())
        self.assertIn(" n timed\n", self.path.read_text())

    def test_session_header_is_written_once_with_identity(self):
        self.log.identity = "A1B2C3D4E5F6"
        self.log.log("n", "one")
        self.log.flush()
        self.log.log("n", "two")
        self.log.flush()
        text = self.path.read_text()
        self.assertEqual(text.count("--- session start"), 1)
        self.assertIn("id A1B2C3D4E5F6", text)

    def test_full_file_rotates_to_old_and_keeps_one_generation(self):
        self.log.MAX_FILE_BYTES = 200
        for i in range(30):
            self.log.log("n", "line %02d" % i)
            self.log.flush()
        self.assertTrue(self.old.exists())
        self.assertLessEqual(self.path.stat().st_size, 200)
        self.assertLessEqual(self.old.stat().st_size, 200)
        combined = self.old.read_text() + self.path.read_text()
        self.assertIn("line 29", self.path.read_text())
        self.assertNotIn("line 00", combined)

    def test_write_failure_does_not_raise(self):
        self.log.LOG_PATH = str(Path(self.dir.name) / "missing" / "x.txt")
        self.log.log("n", "lost")
        self.log.flush()
        self.assertEqual(self.log.lines[-1][-6:], "n lost")


if __name__ == "__main__":
    unittest.main()
