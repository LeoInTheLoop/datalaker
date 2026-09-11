"""A completed fixed Snapshot must not keep waking the model cron."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import resumable


class DemoSnapshotTerminalContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.run_dir = root / "runs"
        self.run_dir.mkdir()
        self.case_file = root / "cases.json"
        self.previous = {key: os.environ.get(key) for key in
                         ("DEMO_SNAPSHOT_GUARD", "DEMO_RUN_DIR", "DEMO_CASES_FILE")}
        os.environ.update({"DEMO_SNAPSHOT_GUARD": "1", "DEMO_RUN_DIR": str(self.run_dir),
                           "DEMO_CASES_FILE": str(self.case_file)})
        self.case_file.write_text(json.dumps({"snapshots": [{
            "id": "credentials", "expected_outcome": {
                "kind": "credential_received", "source_id": "northwind",
            },
        }]}), encoding="utf-8")
        (self.run_dir / "runs.json").write_text(json.dumps({"current": {
            "snapshot_id": "credentials", "state": "case_delivered",
        }}), encoding="utf-8")

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_only_a_recorded_success_notice_closes_the_snapshot(self):
        self.assertFalse(resumable._demo_snapshot_terminal([]))
        self.assertFalse(resumable._demo_snapshot_terminal([
            {"source_id": "northwind", "failed": True},
        ]))
        self.assertTrue(resumable._demo_snapshot_terminal([
            {"source_id": "northwind", "failed": False},
        ]))


if __name__ == "__main__":
    unittest.main()
