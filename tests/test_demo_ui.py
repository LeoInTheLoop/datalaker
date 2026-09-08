#!/usr/bin/env python3
"""Narrow contract checks for the demo adapter; no model, Docker, or database."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from services import demo_ui as ui


class DemoUIContracts(unittest.TestCase):
    def test_fixed_cases_only_expose_compatible_snapshots(self):
        cases, snapshots = ui.cases()
        self.assertIn("northwind-sales", cases)
        self.assertIn("northwind-dirty-status", snapshots)
        self.assertIn("northwind-full", snapshots)
        self.assertEqual(
            snapshots["northwind-full"]["source_tables"],
            ["orders", "order_details", "employees", "demo_order_status"],
        )
        self.assertIn("silver raw 与洗后值", cases["northwind-sales"]["expects"])
        for case in cases.values():
            self.assertTrue(case["snapshots"])
            self.assertTrue(all(item in snapshots for item in case["snapshots"]))

    def test_case_packet_contains_human_history_not_tool_script(self):
        cases, snapshots = ui.cases()
        packet = ui.case_packet(cases["northwind-sales"], snapshots["northwind-base"])
        self.assertIn("此前人员邮件记录", packet["body"])
        self.assertNotIn("answer_with_link(", packet["body"])

    def test_browser_mail_never_receives_approval_token(self):
        visible = ui.public_mail([{"body": "x", "_links": [("id", "secret")], "links": []}])
        self.assertNotIn("_links", visible[0])

    def test_reset_request_is_a_run_scoped_control_file(self):
        original = ui.CONTROL_DIR
        with tempfile.TemporaryDirectory() as tmp:
            ui.CONTROL_DIR = pathlib.Path(tmp)
            ui.request_agent_reset("demo-test-run")
            request = ui.CONTROL_DIR / "requests" / "demo-test-run.json"
            self.assertEqual(json.loads(request.read_text())["run_id"], "demo-test-run")
        ui.CONTROL_DIR = original

    def test_adapter_has_no_governance_tool_or_decision_endpoint(self):
        source = pathlib.Path(ui.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import tools", source)
        self.assertNotIn("/api/tool", source)
        self.assertNotIn("INSERT INTO decisions", source)


if __name__ == "__main__":
    unittest.main()
