"""Demo Snapshot scope is enforced before the model can extend the workflow."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins import datasteward_gate as gate


class SnapshotScopeContracts(unittest.TestCase):
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
            "id": "credentials", "tool_scope": ["connect_source"],
            "terminal_condition": "source connected",
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

    def test_credentials_snapshot_allows_connection_and_blocks_later_steps(self):
        self.assertIsNone(gate._snapshot_scope_block("connect_source"))
        blocked = gate._snapshot_scope_block("list_source_tables")
        self.assertIn("SNAPSHOT_SCOPE", blocked or "")
        self.assertIn("source connected", blocked or "")

    def test_guard_is_inactive_before_the_snapshot_mail_is_delivered(self):
        (self.run_dir / "runs.json").write_text(json.dumps({"current": {
            "snapshot_id": "credentials", "state": "reset_requested",
        }}), encoding="utf-8")
        self.assertIsNone(gate._snapshot_scope_block("list_source_tables"))


if __name__ == "__main__":
    unittest.main()
