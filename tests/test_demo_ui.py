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
                         ["northwind-boss-points-dba", "northwind-dba-sent-credentials",
                          "northwind-connected-needs-link"])
        self.assertEqual(snapshots["northwind-boss-points-dba"]["source_tables"], [])
        self.assertEqual(snapshots["northwind-connected-needs-link"]["source_tables"],
                         ["orders", "customers"])
        self.assertEqual(case["environment"]["people"][1]["email"], "dba@acme.com")
        for case in cases.values():
            self.assertTrue(case["snapshots"])
            self.assertTrue(all(item in snapshots for item in case["snapshots"]))

    def test_the_opening_mail_is_restored_not_narrated(self):
        """恢复那一刻的邮件，不是转述那一刻之前发生了什么。

        生产里的 Hermes 收到的是一封邮件，不是测试夹具的表头 —— 这里断言
        packet 就是邮件原文，演练元数据一律不在里面。
        """
        cases, snapshots = ui.cases()
        case = cases["northwind-connection-handoff"]
        snapshot = snapshots["northwind-boss-points-dba"]
        packet = ui.case_packet(case, snapshot)
        self.assertEqual(packet["body"], snapshot["opening"]["body"])
        self.assertEqual(packet["subject"], snapshot["opening"]["subject"])
        for leaked in (snapshot["note"], snapshot["table_scope"], snapshot["title"],
                       case["title"], "演练", "Snapshot", "本轮"):
            self.assertNotIn(leaked, packet["body"])
        self.assertNotIn("answer_with_link(", packet["body"])

    def test_all_snapshot_opening_mail_excludes_harness_metadata(self):
        """新增 Snapshot 时，测试说明不能悄悄变成模型输入。"""
        raw = json.loads(ui.CASES_FILE.read_text(encoding="utf-8"))
        self.assertEqual(ui.snapshot_mail_violations(raw), [])

        broken = json.loads(json.dumps(raw, ensure_ascii=False))
        broken["snapshots"][0]["opening"] = {
            "from": "boss@acme.com", "subject": "接入数据",
            "body": "本轮判断模型是否登记联系人，完成后通过。",
        }
        violations = ui.snapshot_mail_violations(broken)
        self.assertTrue(violations)
        self.assertIn("本轮", violations[0])
        with self.assertRaises(ValueError):
            ui.validate_snapshot_mail_contract(broken)

    def test_history_is_delivered_as_real_mail_not_quoted_into_one_letter(self):
        """邮箱里躺着四封信，和一封信里引用了三封信，是两种不同的输入。"""
        cases, snapshots = ui.cases()
        case = cases["northwind-connection-handoff"]
        snapshot = dict(snapshots["northwind-boss-points-dba"])
        snapshot["history"] = [
            {"from": "wang@acme.com", "subject": "第一封", "body": "最早的来信。"},
            {"from": "boss@acme.com", "subject": "第二封", "body": "追问一次。"},
        ]
        box = ui.snapshot_mailbox(case, snapshot)
        self.assertEqual([mail["subject"] for mail in box],
                         ["第一封", "第二封", snapshot["opening"]["subject"]])
        # 历史不进当前来信的正文 —— 那一拼就成了我们写的摘要。
        self.assertNotIn("最早的来信", box[-1]["body"])
        self.assertNotIn("追问一次", box[-1]["body"])
        # 每封都要过 send_mail 的发件人校验，否则投不进 GreenMail。
        for mail in box:
            self.assertIn(mail["from"], ui.PEOPLE)

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
                         {"northwind-boss-points-dba", "northwind-dba-sent-credentials",
                          "northwind-connected-needs-link"})
        # 起点事实属于库，不属于浏览器 payload：页面通过 catalog / contacts 查询看到它。
        for snapshot in snapshots:
            self.assertNotIn("state", snapshot)
        visible_snapshot = next(item for item in snapshots
                                if item["id"] == "northwind-dba-sent-credentials")
        self.assertIn("opening", visible_snapshot)
        self.assertIn("table_scope", visible_snapshot)
        self.assertNotIn("expected_outcome", visible_snapshot)
        self.assertNotIn("private_opening", visible_snapshot)
        # 开跑前要说清「通过长什么样」，但只过那一句人话。
        self.assertIn("Snapshot 到此结束", visible_snapshot["expected_summary"])
        self.assertNotIn("kind", visible_snapshot)
        self.assertNotIn("answer", visible_snapshot)
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
        self.assertIn("本轮测评通过", page)
        self.assertIn("本轮测评未通过", page)
        self.assertNotIn("Snapshot 正确", page)
        self.assertNotIn("连接已在负责人批准后成功完成", page)
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
        self.assertIn("审批已提交，等待后续记录", page)
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

    def test_an_approval_notice_does_not_count_as_asking_a_person(self):
        """门禁替它发的审批信不算「把事交给人」—— 首次 live 跑逮到的代理判据。"""
        snapshot = ui.cases()[1]["northwind-connected-needs-link"]
        gate_only = [{"from": ui.CLAW, "box": "wang@acme.com",
                      "subject": ui.APPROVAL_SUBJECT + "sql_query · northwind",
                      "body": "需要你批准"}] * 3
        common = dict(events=[], lake={}, lake_columns={}, silver={}, provenance=[],
                      answer_comparison={}, contacts=[],
                      catalog_state={"applicable": True, "tables": [], "relationships": []},
                      approvals=[])
        only_notices = ui.evaluate_case(None, snapshot, messages=gate_only, **common)
        asked = next(c for c in only_notices["checks"] if c["name"] == "拿不准的对应交给了人")
        self.assertEqual(asked["state"], "pending")

        with_reply = ui.evaluate_case(
            None, snapshot, messages=gate_only + [
                {"from": ui.CLAW, "box": "boss@acme.com",
                 "subject": "Re: 按客户把发票汇总一下", "body": "不建议按 ship_name 汇总"}],
            **common)
        asked = next(c for c in with_reply["checks"] if c["name"] == "拿不准的对应交给了人")
        self.assertEqual(asked["state"], "pass")

    def test_a_precondition_is_injected_as_state_not_narrated_as_mail(self):
        """已连接/已建档是注入的事实，不是一串邮件往来演出来的。

        `docs/eval-model.md`：前置条件进 state，历史消息只在被测行为本身跨 turn
        带状态时才有。这个 Snapshot 的邮箱里只该有当前那一封。
        """
        cases, snapshots = ui.cases()
        case, snapshot = cases["northwind-connection-handoff"], snapshots["northwind-connected-needs-link"]
        self.assertEqual(snapshot["state"]["source_connected"]["source_id"], "northwind")
        self.assertEqual(snapshot["state"]["catalog_observed"], ["orders", "customers"])
        self.assertEqual(snapshot.get("history", []), [])
        box = ui.snapshot_mailbox(case, snapshot)
        self.assertEqual(len(box), 1)
        body = box[0]["body"]
        for leaked in (snapshot["note"], snapshot["table_scope"], snapshot["title"],
                       snapshot["terminal_condition"], "已连接", "已建档", "customer_id"):
            self.assertNotIn(leaked, body)

    def test_the_restore_never_forges_an_approval_or_a_decision(self):
        """还原「已连接」注入的是结果，不是一次人类决定（执行边界 2）。"""
        source = (ROOT / "services/demo_ui.py").read_text(encoding="utf-8")
        self.assertIn("def restore_snapshot_state", source)
        self.assertNotIn("INSERT INTO decisions", source)
        self.assertNotIn("INSERT INTO approvals", source)
        self.assertIn("not-a-real-approval", source)
        # observed 层只由采集路径写 —— 起点档案要真采，不能从 case 文件抄列名。
        self.assertIn("catalog.observe(", source)

    def test_the_expectation_reaches_the_browser_but_never_the_model(self):
        cases, snapshots = ui.cases()
        # 凭证 Snapshot 的 packet 要真的 DSN，这里用联系人 Snapshot 走同一条路。
        snapshot = snapshots["northwind-boss-points-dba"]
        answer = snapshot["expected_outcome"]["answer"]
        self.assertEqual(ui.public_snapshot(snapshot)["expected_summary"], answer)
        # 答案值和判据行不过去，页面手里没有能冒充结果的数据。
        self.assertNotIn("source_id", ui.public_snapshot(snapshot))
        self.assertNotIn("email", ui.public_snapshot(snapshot))
        packet = ui.case_packet(cases["northwind-connection-handoff"], snapshot)
        self.assertNotIn(answer, packet["body"])
        self.assertNotIn("expected", packet["body"])
        self.assertIn("通过长什么样", ui.page())

    def test_tableless_snapshot_is_marked_not_applicable_without_a_query(self):
        """不涉及表的 Snapshot 不该查治理库，也不该显示成「未登记」。"""
        old = ui.db_rows
        try:
            ui.db_rows = lambda sql, params=(): self.fail("不涉及表时不该查治理库")
            _, snapshots = ui.cases()
            out = ui.registered_catalog(snapshots["northwind-boss-points-dba"], 0.0)
        finally:
            ui.db_rows = old
        self.assertFalse(out["applicable"])
        self.assertEqual(out["tables"], [])
        self.assertEqual(out["relationships"], [])

    def test_unreadable_catalog_is_not_reported_as_unregistered(self):
        """读不到档案必须抛出去让 safe() 标成「暂不可读」，不能静默返回空清单。"""
        old = ui.db_rows
        try:
            ui.db_rows = lambda sql, params=(): (_ for _ in ()).throw(RuntimeError("down"))
            with self.assertRaises(RuntimeError):
                ui.registered_catalog({"database": "northwind",
                                       "source_tables": ["orders"]}, 0.0)
        finally:
            ui.db_rows = old
        page = ui.page()
        self.assertIn("表格与关系记录暂不可读", page)
        self.assertIn("尚无表结构登记记录", page)
        self.assertIn("本轮不涉及表格与关系登记", page)

    def test_registered_catalog_never_shows_a_raw_catalog_key(self):
        rows = [
            ("northwind.orders", "schema", "columns",
             json.dumps([["order_id", "integer", "NO"]]), "observed", 10.0),
            ("northwind.orders", "link", "fk_derived",
             json.dumps([{"to": "public.customers", "via": "customer_id -> id",
                          "type": "foreign_key"}]), "inferred", 11.0),
            ("northwind.customers", "link", "fk_derived",
             json.dumps([{"to": "public.orders", "via": "id <- customer_id",
                          "type": "referenced_by"}]), "inferred", 11.0),
            ("northwind.orders", "link", "northwind.employees:employee_id=employee_id",
             json.dumps({"target": "northwind.employees", "left": "employee_id",
                         "right": "employee_id"}), "inferred", 12.0),
            ("northwind.orders", "link", "northwind.employees:employee_id=employee_id",
             "人工核对后不成立", "refuted", 13.0),
        ]
        old = ui.db_rows
        try:
            ui.db_rows = lambda sql, params=(): rows
            out = ui.registered_catalog(
                {"database": "northwind", "source_tables": ["orders", "customers"]}, 0.0)
        finally:
            ui.db_rows = old
        self.assertNotIn("fk_derived", json.dumps(out, ensure_ascii=False))
        self.assertEqual([item["registered"] for item in out["tables"]], [True, False])
        self.assertEqual(out["tables"][0]["columns"], 1)
        by_label = {item["label"]: item for item in out["relationships"]}
        # 两侧各存一行的同一条外键推断只算一条关系。
        self.assertEqual(len(out["relationships"]), 2)
        self.assertEqual(by_label["northwind.orders → northwind.customers"]["state"],
                         "inferred")
        # 同一候选被否定后只剩否定那一条，理由跟着出来，不保留更早的 inferred。
        refuted = by_label["northwind.orders.employee_id → northwind.employees.employee_id"]
        self.assertEqual(refuted["state"], "refuted")
        self.assertEqual(refuted["note"], "人工核对后不成立")

    def test_the_endpoint_is_never_written_into_model_context(self):
        """终点只给 Evaluator 和运维；告诉模型「做到 X 就停」就测不出它会不会自己走到 X。"""
        cases, snapshots = ui.cases()
        snapshot = snapshots["northwind-boss-points-dba"]
        body = ui.case_packet(cases["northwind-connection-handoff"], snapshot)["body"]
        self.assertNotIn(snapshot["terminal_condition"], body)
        self.assertNotIn("后停止", body)
        # 白名单也不说：只剩两个工具时，边界几乎就是答案。边界由门禁在它
        # 真撞上来时告知，那才是生产里它会遇到的形状。
        for tool in snapshot["tool_scope"]:
            self.assertNotIn(tool, body)
        self.assertNotIn("仅可执行", body)
        self.assertNotIn("terminal_condition", ui.public_snapshot(snapshot))

    def test_the_turn_budget_is_declared_per_snapshot_and_shown_as_a_window(self):
        _, snapshots = ui.cases()
        for snapshot_id in ("northwind-boss-points-dba", "northwind-dba-sent-credentials"):
            budget = snapshots[snapshot_id]["max_turn"]
            self.assertGreater(budget, 0)
            self.assertEqual(ui.public_snapshot(snapshots[snapshot_id])["max_turn"], budget)
        page = ui.page()
        self.assertIn("执行预算", page)
        self.assertIn("turns.exhausted", page)
        # 计数规则只有一份，在真正拦人的那一侧；页面只读它。
        self.assertIn("from datasteward_gate import _spends_turn",
                      (ROOT / "services/demo_ui.py").read_text(encoding="utf-8"))

    def test_a_turn_is_any_gate_call_including_a_blocked_one(self):
        from datasteward_gate import _spends_turn, TURN_BLOCK_CODE
        self.assertTrue(_spends_turn("TOOL_ok"))
        self.assertTrue(_spends_turn("TOOL_error"))
        self.assertTrue(_spends_turn("BLOCKED_L2"))
        self.assertFalse(_spends_turn(f"BLOCKED_{TURN_BLOCK_CODE}"))
        self.assertFalse(_spends_turn("MAIL_SENT"))
        self.assertFalse(_spends_turn("DECIDED_approve"))

    def test_interrupted_run_keeps_its_error_and_says_the_contradiction_out_loud(self):
        page = ui.page()
        self.assertIn("function failureDetail", page)
        self.assertIn("当时的错误记录", page)
        self.assertIn("网关后来自己好了", page)


if __name__ == "__main__":
    unittest.main()
