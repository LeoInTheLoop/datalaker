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


class _FakeStore:
    """`rows_of` 按后端类名分支，所以这个桩走 SQLite 那条路。"""
    def __init__(self, kinds):
        self.kinds = kinds
        self.db = self

    def execute(self, sql, params=()):
        assert "%" not in sql, f"psycopg 会把 % 当占位符：{sql}"
        return self

    def fetchall(self):
        return [(kind,) for kind in self.kinds]


class _BrokenStore(_FakeStore):
    def __init__(self):
        super().__init__([])

    def execute(self, sql, params=()):
        raise RuntimeError("ledger unavailable")


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
        # 被拦的文本也是模型上下文 —— 终点不能从这里漏出去（eval-model 红线 2）。
        self.assertNotIn("source connected", blocked or "")

    def test_the_turn_budget_stops_a_run_that_never_reaches_its_endpoint(self):
        self.case_file.write_text(json.dumps({"snapshots": [{
            "id": "credentials", "tool_scope": ["connect_source"], "max_turn": 3,
        }]}), encoding="utf-8")
        (self.run_dir / "runs.json").write_text(json.dumps({"current": {
            "snapshot_id": "credentials", "state": "case_delivered",
            "started_at": 100.0,
        }}), encoding="utf-8")

        kinds: list[str] = []
        store = _FakeStore(kinds)
        # 预算内：放行，门禁不插手。
        self.assertIsNone(gate._snapshot_turn_block(store, "connect_source"))
        # 被 block 的也算一次 turn —— 否则反复撞同一个 block 可以无限循环。
        kinds += ["TOOL_ok", "BLOCKED_L2", "MAIL_SENT", "DECIDED_approve"]
        self.assertIsNone(gate._snapshot_turn_block(store, "connect_source"))
        kinds.append("TOOL_error")
        stopped = gate._snapshot_turn_block(store, "connect_source")
        self.assertIn(gate.TURN_BLOCK_CODE, stopped or "")
        self.assertIn("3", stopped or "")
        # 自己写的那条事件不计入，否则超限之后越算越多。
        kinds += [f"BLOCKED_{gate.TURN_BLOCK_CODE}"] * 5
        self.assertIn("3", gate._snapshot_turn_block(store, "connect_source") or "")

    def test_an_unreadable_turn_count_stops_instead_of_lifting_the_budget(self):
        self.case_file.write_text(json.dumps({"snapshots": [{
            "id": "credentials", "tool_scope": ["connect_source"], "max_turn": 3,
        }]}), encoding="utf-8")
        (self.run_dir / "runs.json").write_text(json.dumps({"current": {
            "snapshot_id": "credentials", "state": "case_delivered",
            "started_at": 100.0,
        }}), encoding="utf-8")
        blocked = gate._snapshot_turn_block(_BrokenStore(), "connect_source")
        self.assertIn(gate.TURN_BLOCK_CODE, blocked or "")
        self.assertIn("fail-closed", blocked or "")

    def test_a_snapshot_without_a_budget_is_not_turn_limited(self):
        self.assertIsNone(gate._snapshot_turn_block(_FakeStore(["TOOL_ok"] * 50),
                                                    "connect_source"))

    def test_guard_is_inactive_before_the_snapshot_mail_is_delivered(self):
        (self.run_dir / "runs.json").write_text(json.dumps({"current": {
            "snapshot_id": "credentials", "state": "reset_requested",
        }}), encoding="utf-8")
        self.assertIsNone(gate._snapshot_scope_block("list_source_tables"))


class MonitorWakeSuppression(unittest.TestCase):
    """被 WIP 挡回的线不该每分钟把模型叫醒一次。

    monitor 靠输出哈希抑制重复唤醒，而 `waited` 是其中唯一会变的量 ——
    它的格子必须够粗，否则哈希每次都不同，抑制永远失效。
    实测 2026-09-13：demo 环境把格子设成 60（调试值），被 WIP 挡回的
    ingest_table 每 2 分钟被唤醒重试，29 个 turn 里 16 个白烧在同一堵墙上。
    """
    def test_the_retry_grain_defaults_to_an_hour(self):
        import importlib, os
        previous = os.environ.pop("CLAW_RESUME_UNIT_SECONDS", None)
        try:
            import ops.resumable as resumable
            importlib.reload(resumable)
            self.assertEqual(resumable.RETRY_UNIT_S, 3600)
            row = {"updated_at": __import__("time").time() - 120}
            # 两分钟前更新过：同一格里必须逐字节相同，否则 monitor 会再叫一次。
            self.assertEqual(resumable._waited(row), resumable._waited(row))
            self.assertTrue(resumable._waited(row).endswith("h"))
        finally:
            if previous is not None:
                os.environ["CLAW_RESUME_UNIT_SECONDS"] = previous

    def test_the_demo_environment_does_not_ship_the_debug_grain(self):
        compose = (ROOT / "infra/docker-compose.demo.yml").read_text(encoding="utf-8")
        for line in compose.splitlines():
            stripped = line.strip()
            if stripped.startswith("CLAW_RESUME_UNIT_SECONDS"):
                self.fail(f"演示环境不该覆盖重试格子：{stripped}")


if __name__ == "__main__":
    unittest.main()
