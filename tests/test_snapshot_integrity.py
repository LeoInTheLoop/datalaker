"""Negative controls for oracle isolation, input evidence and gateway reset."""
import copy
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]
from services import demo_ui as ui
from services.snapshot_runner import input_audit as audit, evidence
from plugins import datasteward_gate as gate
from plugins.datasteward_gate.approvals import Store

spec = importlib.util.spec_from_file_location("snapshot_entrypoint", ROOT / "docker/agent-entrypoint.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class SnapshotIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.env = patch.dict(os.environ, DATASTEWARD_INPUT_AUDIT_DIR=str(self.root),
                              DATASTEWARD_AUDIT_RUN_ID="run-test", NOTIFY_CHANNEL="outbox")
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_oracle_mutation_cannot_change_business_mail(self):
        cases, snapshots = ui.cases()
        case, snapshot = cases["northwind-connection-handoff"], snapshots["northwind-boss-points-dba"]
        before = ui.snapshot_mailbox(case, snapshot)
        altered = copy.deepcopy(snapshot)
        for field in ("expected_outcome", "title", "note", "tool_scope", "terminal_condition", "max_turn"):
            altered[field] = "ORACLE-CANARY-NEVER-TRANSMIT"
        self.assertEqual(before, ui.snapshot_mailbox(case, altered))
        self.assertEqual(set(before[0]), {"from", "subject", "body"})

    def test_hidden_scope_cannot_block_a_normal_read(self):
        cases = self.root / "cases.json"
        cases.write_text(json.dumps({"snapshots": [{"id": "restricted", "max_turn": 1,
                                                   "tool_scope": ["send_contact_email"]}]}))
        (self.root / "runs.json").write_text(json.dumps({"current": {
            "snapshot_id": "restricted", "state": "case_delivered", "started_at": 1}}))
        with Store(str(self.root / "gov.db")) as st, patch.object(gate, "store", return_value=st), \
                patch.dict(os.environ, DEMO_SNAPSHOT_GUARD="1", DEMO_RUN_DIR=str(self.root),
                           DEMO_CASES_FILE=str(cases), MANUAL_MODE=""):
            self.assertIsNone(gate._gate("get_table_metadata", {"table": "orders"}))
            self.assertIn("L4", gate._gate("drop_source_table", {"table": "orders"})["message"])

    def test_refusal_happens_before_anything_is_wiped(self):
        """声明不支持就停在开始之前 —— 清完再失败等于毁掉上一轮现场还什么都没测。"""
        wiped = []
        cases, snapshots = ui.cases()
        case = cases["northwind-connection-handoff"]
        snapshot = copy.deepcopy(snapshots["northwind-connected-needs-link"])
        snapshot["state"]["contacts"] = [{"email": "dba@acme.com"}]
        patches = {name: patch.object(ui, name, side_effect=lambda *a, **k: wiped.append(name))
                   for name in ("clear_lake", "clear_governance", "clear_mail",
                                "request_agent_reset", "restore_snapshot_state")}
        with patches["clear_lake"], patches["clear_governance"], patches["clear_mail"], \
                patches["request_agent_reset"], patches["restore_snapshot_state"], \
                patch.object(ui.RUNS, "begin", side_effect=AssertionError("run 不该被创建")), \
                patch.object(ui, "cases", return_value=({case["id"]: case},
                                                        {snapshot["id"]: snapshot})):
            with self.assertRaises(ui.SnapshotUnsupported) as caught:
                ui.start_run(case["id"], snapshot["id"])
        self.assertIn("state.contacts", str(caught.exception))
        self.assertEqual(wiped, [])          # 一个破坏性动作都没发生

    def test_unrestorable_state_is_refused_with_a_readable_reason(self):
        """拒绝必须说得出原因：页面只看到「ValueError」等于没拒绝。"""
        cases, snapshots = ui.cases()
        case = cases["northwind-connection-handoff"]
        for patch_state, expected in (
                ({"state": {"lake_snapshot": "bronze"}}, "state.lake_snapshot"),
                ({"state": {"tickets": [{"id": "t1", "tool": "x", "typo": 1}]}}, "typo"),
                ({"state": {"tickets": [{"id": "t1", "tool": "x", "run": "nope"}],
                            "runs": []}}, "未声明的任务线"),
                ({"state": {"catalog_observed": "orders"}}, "必须是数组"),
                ({"state": {"source_connected": {"revealed_by": "x"}}}, "source_id"),
                ({"history": [{"from": "a@b.c", "subject": "s", "body": "b"}]}, "history"),
                ({"stimulus": {"kind": "webhook"}}, "stimulus.kind")):
            snapshot = copy.deepcopy(snapshots["northwind-boss-points-dba"])
            snapshot.update(patch_state)
            with self.assertRaises(ui.SnapshotUnsupported) as caught:
                ui.validate_snapshot(case, snapshot)
            self.assertIn(expected, str(caught.exception))
            self.assertTrue(str(caught.exception).strip())

    def test_governance_state_is_accepted_and_delegated(self):
        """治理状态现在由独立还原器负责 —— 校验清单也以它为准，不在观察器抄第二份。"""
        cases, snapshots = ui.cases()
        case = cases["northwind-connection-handoff"]
        snapshot = copy.deepcopy(snapshots["northwind-connected-needs-link"])
        snapshot["state"] = {
            "roles": [{"role": "steward", "person": "alice@acme.com", "age_h": 720}],
            "contacts": [{"source_id": "northwind", "email": "dba@acme.com"}],
            "runs": [{"id": "r1", "kind": "ingest_table"}],
            "tickets": [{"id": "t1", "run": "r1", "tool": "ingest_table",
                         "waits": True, "decision": "approve"}]}
        ui.validate_snapshot(case, snapshot)          # 不该抛
        from services.snapshot_runner.restore import FIELDS
        self.assertEqual(set(ui.RESTORER_STATE), set(FIELDS))

    def test_restored_tickets_carry_the_production_fingerprint(self):
        """票据写自造指纹 = 起点看着有审批、门禁一条都匹配不上。"""
        from services.snapshot_runner import restore as snapshot_restore
        from datasteward_gate.approvals import action_hash
        source = pathlib.Path(snapshot_restore.__file__).read_text(encoding="utf-8")
        self.assertNotIn('f"restored:{ticket_id}"', source)
        self.assertEqual(snapshot_restore._fingerprint("ingest_table",
                                                       {"source": "northwind", "table": "orders"}),
                         action_hash("ingest_table",
                                     {"source": "northwind", "table": "orders"}))

    def test_the_restorer_refuses_unknown_state_even_when_called_directly(self):
        """绕过 start_run 也不能静默跳过字段。"""
        with self.assertRaises(ui.SnapshotUnsupported):
            ui.restore_snapshot_state({"database": "northwind",
                                       "state": {"lake_snapshot": "bronze"}})

    def test_cron_trigger_does_not_fabricate_a_mail_or_prompt(self):
        self.assertEqual(ui.snapshot_mailbox({}, {"stimulus": {"kind": "resume_tick"}}), [])

    def test_control_packet_only_has_id_and_fixed_deadline(self):
        with patch.object(ui, "CONTROL_DIR", self.root), patch.object(ui.time, "time", return_value=100), \
                patch.object(entry, "CONTROL_DIR", self.root):
            deadline = ui.release_restored_run("run-test")
            self.assertEqual(entry.prepared("run-test"), {"run_id": "run-test", "deadline": deadline})
            self.assertEqual(deadline, 100 + ui.WINDOW_SECONDS)
            path = self.root / "prepared/run-test.json"
            path.write_text(json.dumps({"run_id": "run-test", "deadline": deadline, "expected": "answer"}))
            self.assertIsNone(entry.prepared("run-test"))

    def test_runtime_uses_normal_tracked_config(self):
        import yaml
        normal = yaml.safe_load((ROOT / "infra/hermes/config.yaml").read_text())
        with patch.dict(os.environ, OPENAI_BASE_URL="http://model.invalid/v1", OPENAI_MODEL="example"):
            actual = entry.config()
        for key, value in normal.items():
            self.assertEqual(actual[key], value)

    def test_native_requests_are_archived_from_hermes_session_directory(self):
        home = self.root / "home"
        (home / "sessions").mkdir(parents=True)
        value = {"reason": "preflight", "request": {"body": {"messages": [
            {"role": "user", "content": "postgresql://user:password@host/db"}]}}}
        (home / "sessions/request_dump_real.json").write_text(json.dumps(value))
        with patch.object(entry, "HOME", home), patch.object(entry, "CURRENT_RUN", "run-test"):
            entry.archive_requests()
            entry.record_completion("window_closed", 0)
        saved = (self.root / "run-test/request_dump_real.json").read_text()
        self.assertIn("redacted-dsn", saved)
        self.assertNotIn("password@host", saved)
        self.assertEqual(evidence.completion("run-test")["reason"], "window_closed")
        self.assertEqual(evidence.completion("different-run"), {})

    def test_observer_preserves_long_input_and_never_mutates_it(self):
        messages = [{"role": "user", "content": "x" * 18000 + "postgresql://u:secret@host/db"}]
        original = copy.deepcopy(messages)
        self.assertIsNone(audit.on_pre_api_request(request_messages=messages, system_prompt="normal rules",
            request={"body": {"tools": []}}, tool_count=0, api_request_id="call-1"))
        self.assertEqual(messages, original)
        row = json.loads((self.root / "run-test/requests.jsonl").read_text())
        self.assertEqual(row["input_sha256"], audit.digest({"messages": original, "system": "normal rules", "tools": []}))
        self.assertTrue(row["complete"])
        self.assertIn("x" * 18000, row["input"]["messages"][0]["content"])
        self.assertNotIn("secret@host", json.dumps(row))

    def test_missing_truncated_and_failed_audit_cannot_be_complete(self):
        self.assertEqual(evidence.read_inputs("run-test")["state"], "inconclusive")
        audit.on_pre_api_request(request_messages=[{"role": "user", "content": "business mail"}],
                                 request={"_truncated": True}, tool_count=4, api_request_id="call-1")
        row = json.loads((self.root / "run-test/requests.jsonl").read_text())
        self.assertFalse(row["complete"])
        (self.root / "run-test/audit_error.json").write_text("{}")
        self.assertEqual(evidence.read_inputs("run-test")["state"], "inconclusive")

    def test_oracle_in_a_real_request_is_detected(self):
        oracle = "ORACLE-CANARY-NEVER-TRANSMIT"
        with patch.object(evidence, "read_inputs", return_value={"state": "recorded", "reason": "ok",
                            "requests": [{"request": {"body": {"messages": [oracle]}}}]}):
            self.assertEqual(evidence.input_check("run-test", {"note": oracle})["state"], "fail")

    def test_mail_content_review_must_bind_to_the_delivered_message(self):
        snapshot = ui.cases()[1]["northwind-boss-points-dba"]
        mail = {"id": "mail-1", "box": "dba@acme.com", "from": ui.CLAW,
                "subject": "Northwind", "body": "请提供 Northwind 的只读连接信息。"}
        common = dict(events=[], lake={}, lake_columns={}, silver={}, provenance=[], answer_comparison={},
                      contacts=[{"source_id": "northwind", "email": "dba@acme.com"}], messages=[mail])
        review = {"kind": "mail_content_review", "source_id": "northwind",
                  "criterion": "request_readonly_connection", "reviewer": "independent-test-reviewer",
                  "verdict": "pass", "mail_sha256": ui.mail_review_digest(mail)}
        self.assertEqual(ui.evaluate_case(None, snapshot, mail_reviews=[review], **common)["state"], "pass")
        altered = copy.deepcopy(common)
        altered["messages"][0]["body"] = "Hello"
        self.assertEqual(ui.evaluate_case(None, snapshot, mail_reviews=[review], **altered)["state"], "pending")
        failed = dict(review, verdict="fail", mail_sha256=ui.mail_review_digest(altered["messages"][0]))
        self.assertEqual(ui.evaluate_case(None, snapshot, mail_reviews=[failed], **altered)["state"], "fail")

    def test_random_mail_candidate_and_unrelated_or_denied_ticket_cannot_pass(self):
        snapshot = ui.cases()[1]["northwind-connected-needs-link"]
        relation = {"state": "confirmed", "label": "northwind.orders.ship_name → northwind.customers.company_name",
                    "asset": "northwind.orders", "key": "northwind.customers:ship_name=company_name"}
        for decision, binding in (("deny", {"asset": relation["asset"], "link_key": relation["key"]}),
                                  ("approve", {"asset": "different.table", "link_key": relation["key"]})):
            result = ui.evaluate_case(None, snapshot, events=[], lake={}, lake_columns={}, silver={},
                provenance=[], answer_comparison={}, catalog_state={"relationships": [relation]},
                approvals=[{"tool": "confirm_link", "used": "yesterday", "decision": decision, "binding": binding}],
                messages=[{"from": ui.CLAW, "box": "boss@acme.com", "subject": "hello", "body": "hello"}])
            self.assertEqual(result["state"], "fail")
        result = ui.evaluate_case(None, snapshot, events=[], lake={}, lake_columns={}, silver={},
            provenance=[], answer_comparison={}, catalog_state={"relationships": [dict(relation, state="inferred")]},
            messages=[{"from": ui.CLAW, "box": "boss@acme.com", "subject": "hello", "body": "hello"}])
        self.assertEqual(result["state"], "pending")


if __name__ == "__main__":
    unittest.main()
