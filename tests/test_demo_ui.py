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
        self.assertEqual(set(cases), {"northwind-connection-handoff"})
        case = cases["northwind-connection-handoff"]
        self.assertEqual(case["snapshots"],
                         ["northwind-boss-points-dba", "northwind-dba-sent-credentials"])
        self.assertEqual(snapshots["northwind-boss-points-dba"]["source_tables"], [])
        self.assertEqual(case["environment"]["people"][1]["email"], "dba@acme.com")
        for case in cases.values():
            self.assertTrue(case["snapshots"])
            self.assertTrue(all(item in snapshots for item in case["snapshots"]))

    def test_case_packet_contains_human_history_not_tool_script(self):
        cases, snapshots = ui.cases()
        packet = ui.case_packet(cases["northwind-connection-handoff"],
                                snapshots["northwind-boss-points-dba"])
        self.assertIn("此前人员邮件记录", packet["body"])
        self.assertIn("本轮表范围", packet["body"])
        self.assertIn("不指定、不读取、不接入任何业务表", packet["body"])
        self.assertNotIn("answer_with_link(", packet["body"])

    def test_snapshot_view_only_receives_snapshot_state(self):
        """Full-simulation staging remains server-side until that view exists."""
        visible = ui.public_cases()
        self.assertEqual({case["id"] for case in visible}, {"northwind-connection-handoff"})
        for case in visible:
            self.assertIn("environment", case)
            self.assertNotIn("description", case)
            self.assertNotIn("history", case)
            self.assertNotIn("opening", case)
            self.assertNotIn("you_play", case)
            self.assertNotIn("steps", case)
            self.assertNotIn("replies", case)
            self.assertNotIn("expected_outcome", case)
        snapshots = ui.public_snapshots()
        self.assertEqual({snapshot["id"] for snapshot in snapshots},
                         {"northwind-boss-points-dba", "northwind-dba-sent-credentials"})
        visible_snapshot = next(item for item in snapshots
                                if item["id"] == "northwind-dba-sent-credentials")
        self.assertIn("opening", visible_snapshot)
        self.assertIn("table_scope", visible_snapshot)
        self.assertNotIn("expected_outcome", visible_snapshot)
        self.assertNotIn("private_opening", visible_snapshot)
        self.assertNotIn("tool_scope", visible_snapshot)
        self.assertNotIn("terminal_condition", visible_snapshot)
        self.assertNotIn("expected_outcome", ui.public_snapshot(
            ui.cases()[1]["northwind-dba-sent-credentials"]))

    def test_snapshot_page_does_not_render_a_script_or_canned_reply(self):
        page = ui.page()
        self.assertIn("Snapshot Case", page)
        self.assertIn("id=actions", page)
        self.assertIn("id=backendToggle", page)
        self.assertIn("默认只看对话和人工审批", page)
        self.assertIn("Snapshot 判定", page)
        self.assertIn("Snapshot 已完成", page)
        self.assertIn("本 Snapshot 已在终点收口", page)
        self.assertNotIn("data-reply", page)
        self.assertNotIn("会发生什么：", page)
        self.assertNotIn("你扮演：", page)

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

    def test_timeline_panel_renders_snapshot_and_live_events(self):
        page = ui.page()
        self.assertIn("对话时间线", page)
        self.assertIn("renderTimeline", page)
        self.assertIn("Snapshot 历史", page)
        self.assertIn("function mailRecipient", page)
        self.assertIn("发给 ' + mailRecipient", page)
        self.assertIn("连接结果", page)
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

    def test_full_simulation_script_never_reaches_the_model(self):
        """you_play / steps / replies are later staging, never model input."""
        raw = json.loads(ui.CASES_FILE.read_text(encoding="utf-8"))
        self.assertTrue(raw["staging_cases"])
        staging = raw["staging_cases"][0]
        self.assertTrue(staging["steps"] and staging["replies"] and staging["you_play"])
        cases, snapshots = ui.cases()
        body = ui.case_packet(cases["northwind-connection-handoff"],
                              snapshots["northwind-boss-points-dba"])["body"]
        self.assertNotIn(staging["you_play"], body)
        for step in staging["steps"]:
            self.assertNotIn(step, body)
        for reply in staging["replies"]:
            self.assertNotIn(reply["body"], body)

    def test_case_replies_use_a_credential_placeholder(self):
        raw = ui.CASES_FILE.read_text(encoding="utf-8")
        self.assertIn("<口令>", raw)
        self.assertNotIn("Opsread7", raw)

    def test_approval_summary_never_exposes_connection_details(self):
        summary = ui.approval_summary("connect_source", {
            "source_id": "northwind", "dsn": "postgresql://reader:secret@db/demo"})
        self.assertIn("northwind", summary)
        self.assertNotIn("secret", summary)
        self.assertNotIn("postgresql", summary)

    def test_page_matches_approval_mail_to_its_ticket_id(self):
        page = ui.page()
        self.assertIn("approvalId", page)
        self.assertIn("startsWith(approvalId)", page)
        self.assertIn("show only the newest real mail", page)
        self.assertIn("function mailAt", page)

    def test_approval_follow_up_marks_only_new_real_records(self):
        page = ui.page()
        self.assertIn("function beginFollowUp", page)
        self.assertIn("function observeFollowUp", page)
        self.assertIn("审批已提交，管家正在继续", page)
        self.assertIn("之后新出现的内容会标「新」", page)

    def test_active_case_can_switch_to_a_new_snapshot_without_a_stale_refresh(self):
        page = ui.page()
        self.assertIn("开始新的 Snapshot Case", page)
        self.assertIn("choosingNewCase", page)
        self.assertIn("当前运行不会被改动", page)
        self.assertIn("Snapshot（选择当前时刻）", page)
        self.assertIn("已启动的 Snapshot", page)

    def test_snapshot_preflight_is_read_only_and_reports_readiness(self):
        old_trino, old_admin = ui.trino, ui.admin_rows
        try:
            ui.trino = lambda sql: ["1"]
            ui.admin_rows = lambda sql: [(1,)]
            self.assertTrue(ui.snapshot_dependencies_ready())
            ui.trino = lambda sql: (_ for _ in ()).throw(RuntimeError("starting"))
            self.assertFalse(ui.snapshot_dependencies_ready())
        finally:
            ui.trino, ui.admin_rows = old_trino, old_admin

    def test_case_judge_requires_the_declared_sales_answer(self):
        case = {"expected_outcome": {"kind": "sales_leader",
                                      "answer": "目标答案",
                                      "row": ["Peacock (4)", "232890.85", "156", "420"]}}
        result = ui.evaluate_case(
            case, events=[], lake={}, lake_columns={}, silver={},
            provenance={}, answer_comparison={
                "model_rows": [["Peacock (4)", "232890.85", "156", "420"]],
                "source_rows": [["x"]] * 9, "matched": True})
        self.assertEqual(result["state"], "pass")
        wrong = ui.evaluate_case(
            case, events=[], lake={}, lake_columns={}, silver={},
            provenance={}, answer_comparison={
                "model_rows": [["Davolio (1)", "192107.60", "123", "345"]],
                "source_rows": [["x"]] * 9, "matched": False})
        self.assertEqual(wrong["state"], "fail")

    def test_case_judge_rejects_raw_columns_in_gold(self):
        case = {"expected_outcome": {"kind": "clean_publish",
                                      "asset": "northwind.demo_order_status",
                                      "allowed_values": ["DELIVERED", "PENDING"]}}
        result = ui.evaluate_case(
            case, events=[],
            lake={"bronze": {"demo_order_status": 24}, "gold": {"status": 24}},
            lake_columns={"gold": {"status": ["order_status", "order_status_raw"]}},
            silver={"samples": [["1", " delivered ", "DELIVERED"]]},
            provenance=[{"asset": "northwind.demo_order_status", "event": "semantics_defined",
                         "detail": {"asset": "northwind.demo_order_status",
                                    "allowed_values": ["DELIVERED", "PENDING"]}}],
            answer_comparison={})
        self.assertEqual(result["state"], "fail")

    def test_contact_case_requires_real_directory_and_a_inbox_delivery(self):
        cases, snapshots = ui.cases()
        result = ui.evaluate_case(
            cases["northwind-connection-handoff"], snapshots["northwind-boss-points-dba"],
            events=[], lake={}, lake_columns={}, silver={},
            provenance=[], answer_comparison={},
            contacts=[{"source_id": "northwind", "email": "dba@acme.com"}],
            messages=[{"box": "dba@acme.com", "from": "claw@acme.test"}])
        self.assertEqual(result["state"], "pass")

    def test_credential_case_requires_a_real_successful_connection(self):
        cases, snapshots = ui.cases()
        waiting = ui.evaluate_case(
            cases["northwind-connection-handoff"], snapshots["northwind-dba-sent-credentials"],
            lake={}, lake_columns={}, silver={},
            provenance=[], answer_comparison={}, contacts=[],
            messages=[{"box": "claw@acme.test", "from": "dba@acme.com",
                       "body": "[数据库连接串已隐藏]"}],
            events=[{"kind": "BLOCKED_PENDING_APPROVAL",
                     "payload": json.dumps({"tool": "connect_source"})}])
        self.assertEqual(waiting["state"], "pending")
        result = ui.evaluate_case(
            cases["northwind-connection-handoff"], snapshots["northwind-dba-sent-credentials"],
            lake={}, lake_columns={}, silver={},
            provenance=[], answer_comparison={}, contacts=[],
            messages=[{"box": "claw@acme.test", "from": "dba@acme.com",
                      "body": "[数据库连接串已隐藏]"},
                      {"box": "dba@acme.com", "from": "claw@acme.test",
                       "subject": "[数据管家] northwind 连接成功（未接入业务表） [#thread]",
                       "body": "Northwind 的只读连接验证成功。"}],
            events=[{"kind": "TOOL_ok", "payload": json.dumps({"tool": "connect_source"})},
                    {"kind": "SOURCE_CONNECT_NOTICE_SENT",
                     "payload": json.dumps({"source_id": "northwind", "failed": False,
                                              "to": ["dba@acme.com"],
                                              "subject": "[数据管家] northwind 连接成功（未接入业务表）"})}])
        self.assertEqual(result["state"], "pass")
        self.assertNotIn("Opsread7", ui.CASES_FILE.read_text(encoding="utf-8"))

    def test_contact_mail_tool_is_declared_and_has_a_secret_egress_boundary(self):
        tool_source = (ROOT / ".hermes/plugins/data-steward/tools.py").read_text(encoding="utf-8")
        policy_source = (ROOT / "plugins/datasteward_gate/policy.py").read_text(encoding="utf-8")
        self.assertIn('"send_contact_email"', tool_source)
        self.assertIn('"send_contact_email"', policy_source)
        self.assertIn("source_contacts", tool_source)
        self.assertIn("不能包含连接串、口令、token 或审批链接", tool_source)


if __name__ == "__main__":
    unittest.main()
