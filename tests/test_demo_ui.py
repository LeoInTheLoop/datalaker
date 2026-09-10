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

    def test_page_is_a_separate_reviewable_file(self):
        page = ui.page()
        self.assertTrue(page.startswith("<!doctype html"))
        self.assertIn("id=timeline", page)
        # The old inline template needed two str.replace escape fixups that
        # broke the whole script whenever either one stopped matching.
        source = pathlib.Path(ui.__file__).read_text(encoding="utf-8")
        self.assertNotIn("HTML = HTML.replace(", source)

    def test_timeline_panel_renders_case_and_live_events(self):
        page = ui.page()
        self.assertIn("对话时间线", page)
        self.assertIn("renderTimeline", page)
        self.assertIn("Case 预设", page)
        self.assertIn("TOOL_", page)

    def test_page_translates_machine_state_into_plain_language(self):
        """A visitor must not have to read run-state or tool identifiers."""
        page = ui.page()
        for token in ("gateway_blocked", "reset_timeout", "snapshot_failed"):
            self.assertIn(token, page)
        for token in ("ingest_table", "define_semantics", "publish_gold"):
            self.assertIn(token, page)
        self.assertIn("本 Case 不涉及", page)          # n/a is not a failure
        self.assertIn("还没走到这步", page)            # pending is not a failure

    def test_page_has_no_governance_or_decision_endpoint(self):
        page = ui.page()
        self.assertNotIn("/api/tool", page)
        self.assertNotIn("INSERT INTO decisions", page)

    def test_case_script_and_replies_never_reach_the_model(self):
        """you_play / steps / replies are page-side staging, not model input."""
        cases, snapshots = ui.cases()
        case = cases["northwind-sales"]
        self.assertTrue(case["steps"] and case["replies"] and case["you_play"])
        body = ui.case_packet(case, snapshots["northwind-base"])["body"]
        self.assertNotIn(case["you_play"], body)
        for step in case["steps"]:
            self.assertNotIn(step, body)
        for reply in case["replies"]:
            self.assertNotIn(reply["body"], body)

    def test_case_replies_use_a_credential_placeholder(self):
        raw = ui.CASES_FILE.read_text(encoding="utf-8")
        self.assertIn("<口令>", raw)
        self.assertNotIn("Opsread7", raw)


if __name__ == "__main__":
    unittest.main()
