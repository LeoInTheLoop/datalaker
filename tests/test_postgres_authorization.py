#!/usr/bin/env python3
"""PostgreSQL authorization contracts for the governance boundary.

These tests intentionally use the two database roles rather than a mocked
store.  They are skipped when the local Postgres fixture is not available;
the full regression runner only invokes them when source_pg is up.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import psycopg
except ImportError:  # pragma: no cover - exercised only in minimal installs
    psycopg = None

from plugins.datasteward_gate.approvals import PgStore, action_hash


def _dsn(role: str) -> str:
    keys = (
        ("DATASTEWARD_AGENT_DSN", "STEWARD_AGENT_DSN")
        if role == "agent"
        else ("DATASTEWARD_APPROVER_DSN", "STEWARD_APPROVER_DSN")
    )
    configured = next((os.environ.get(key) for key in keys if os.environ.get(key)), None)
    if configured:
        return configured
    if role == "agent":
        return os.environ.get(
            "DATASTEWARD_DSN",
            "postgresql://agent_role:agent_pass@127.0.0.1:5432/steward",
        )
    return "postgresql://approver_role:approver_pass@127.0.0.1:5432/steward"


class PostgresAuthorizationContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if psycopg is None:
            raise unittest.SkipTest("psycopg 未安装")
        try:
            with psycopg.connect(_dsn("agent"), connect_timeout=2):
                pass
            with psycopg.connect(_dsn("approver"), connect_timeout=2):
                pass
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Postgres 不可用：{type(exc).__name__}") from exc

    @staticmethod
    def _new_request(agent: PgStore, tool: str, args: dict):
        run_id = f"test-{uuid.uuid4()}"
        fingerprint = action_hash(tool, args)
        return agent.request(run_id, fingerprint, tool, json.dumps(args), "owner")

    def test_postgres_approval_id_is_uuid_and_survives_cross_role_decision(self):
        agent = PgStore(_dsn("agent"))
        approver = PgStore(_dsn("approver"))
        try:
            aid, created = self._new_request(
                agent, "test_uuid_approval", {"nonce": str(uuid.uuid4())}
            )
            self.assertTrue(created)
            parsed = uuid.UUID(str(aid))
            self.assertEqual(str(parsed), str(aid))
            self.assertTrue(approver.decide(str(parsed), "approve", "owner"))
        finally:
            agent.close()
            approver.close()

    def test_amend_pending_changes_only_undecided_ticket(self):
        agent = PgStore(_dsn("agent"))
        approver = PgStore(_dsn("approver"))
        try:
            original = {"source_id": f"test-{uuid.uuid4()}", "dsn": "old"}
            aid, created = self._new_request(agent, "connect_source", original)
            self.assertTrue(created)
            replacement = {**original, "dsn": "new"}
            self.assertTrue(agent.amend_pending(aid, json.dumps(replacement)))
            self.assertTrue(approver.decide(aid, "approve", "owner"))
            self.assertFalse(agent.amend_pending(aid, json.dumps({**original, "dsn": "late"})))
            with agent.db.cursor() as cur:
                cur.execute("SELECT args_json->>'dsn' FROM approvals WHERE id=%s", (aid,))
                self.assertEqual(cur.fetchone()[0], "new")
        finally:
            agent.close()
            approver.close()

    def test_agent_cannot_select_source_secrets_but_controlled_function_returns_one_row(self):
        agent = PgStore(_dsn("agent"))
        approver = PgStore(_dsn("approver"))
        source_id = f"test-source-{uuid.uuid4()}"
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with agent.db.cursor() as cur:
                    cur.execute("SELECT dsn FROM source_secrets WHERE source_id=%s", (source_id,))
                    cur.fetchall()

            secret = f"postgresql://test_user:test_password@source.invalid/{source_id}"
            with approver.db.cursor() as cur:
                cur.execute(
                    "INSERT INTO source_secrets "
                    "(source_id,dsn,kind,approval_id,registered_by,registered_at) "
                    "VALUES (%s,%s,'postgres',%s,'test',extract(epoch from now()))",
                    (source_id, secret, str(uuid.uuid4())),
                )
            with agent.db.cursor() as cur:
                cur.execute("SELECT dsn, kind FROM datasteward_get_source_secret(%s)", (source_id,))
                rows = cur.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], secret)
            self.assertEqual(rows[0][1], "postgres")
        finally:
            agent.close()
            approver.close()


if __name__ == "__main__":
    unittest.main()
