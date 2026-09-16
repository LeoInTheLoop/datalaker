"""Hidden outcomes must never suppress a production monitor wakeup."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ops import resumable


class MonitorIsolation(unittest.TestCase):
    def test_hidden_terminal_outcome_cannot_change_due_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cases = root / "cases.json"
            (root / "runs.json").write_text(json.dumps({"current": {
                "snapshot_id": "credentials", "state": "case_delivered"}}))
            run = {"run_id": "actual-due-task", "kind": "get_table_metadata", "params": {"table": "orders"}}
            fake = types.SimpleNamespace(resumable=lambda: [], retryable=lambda: [], due=lambda: [run])
            outputs = []
            with patch.dict(sys.modules, runs=fake), patch.object(resumable, "_silver_ready", return_value=[]), \
                    patch.object(resumable, "_stop_point", return_value=[]), \
                    patch.dict(os.environ, DEMO_SNAPSHOT_GUARD="1", DEMO_RUN_DIR=directory,
                               DEMO_CASES_FILE=str(cases)):
                for expected in ({"kind": "credential_received", "source_id": "northwind"},
                                 {"kind": "pretend-success", "answer": "do not wake"}):
                    cases.write_text(json.dumps({"snapshots": [{"id": "credentials",
                                                               "expected_outcome": expected}]}))
                    stream = io.StringIO()
                    with contextlib.redirect_stdout(stream):
                        self.assertEqual(resumable.main(), 0)
                    outputs.append(stream.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            self.assertIn("actual-due-task", outputs[0])
            self.assertIn("due", outputs[0])


if __name__ == "__main__":
    unittest.main()
