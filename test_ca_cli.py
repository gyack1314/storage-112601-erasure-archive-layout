"""CLI 与演示入口的端到端测试。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cold_archive.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.tmp.name) / "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(list(argv))
        payload = json.loads(buffer.getvalue())
        return code, payload

    def test_full_lifecycle_and_demo(self):
        code, payload = self._run("init", self.root)
        self.assertEqual(code, 0)
        for name, domain in [("da", "X"), ("db", "Y"), ("dc", "Z"),
                             ("dd", "W"), ("de", "V")]:
            self.assertEqual(self._run("disk", self.root, "--name", name,
                                       "--domain", domain, "--free", "4096")[0], 0)

        code, payload = self._run(
            "put", self.root, "--key", "log", "--rev", "1", "--hex", "a1b2c3d4e5"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["state"], "committed")

        self.assertEqual(self._run("fault", self.root, "--disk", "da")[0], 0)
        code, payload = self._run(
            "download", self.root, "--holder", "ops",
            "--key", "log", "--rev", "1"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["bytes"], "a1b2c3d4e5")

        self._run("fault", self.root, "--disk", "db")
        code, payload = self._run(
            "download", self.root, "--holder", "lost",
            "--key", "log", "--rev", "1"
        )
        self.assertEqual(code, 1)
        self.assertIn("缺失维度", payload["error"])

        code, payload = self._run("inspect", self.root, "--key", "log")
        self.assertEqual(code, 0)
        self.assertIn("revs", payload["result"])

        code, payload = self._run("demo", self.root, "--hex", "0f1e2d")
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["steps"])
        self.assertTrue(payload["result"]["failures_demonstrated"])

    def test_replay_command(self):
        code, payload = self._run(
            "replay", self.root,
            str(Path(__file__).with_name("examples") / "revive-with-one-piece.jsonl"),
        )
        self.assertEqual(code, 0)
        events = payload["result"]["receipts"][0]["events"]
        self.assertEqual(len(events), 13)


if __name__ == "__main__":
    unittest.main()
