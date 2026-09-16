"""Opt-in real Postgres + GreenMail tests, isolated from demo business records.

SNAPSHOT_LIVE_TESTS=1 python -m unittest discover -s tests -p test_snapshot_runner_live.py
Creates/drops its own database and its own mail container. No model involved.
"""
import base64
import imaplib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]
from services.snapshot_runner import mailbox, restore

ENABLED = os.environ.get("SNAPSHOT_LIVE_TESTS") == "1"
BASELINE = {"kind": "bundle", "origin": "snapshot:test-fixture/cold", "sha256": "test"}
ORIGIN = "derived:snapshot:test-fixture/cold/change"


@unittest.skipUnless(ENABLED, "set SNAPSHOT_LIVE_TESTS=1 for isolated real services")
class PostgresOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.psycopg = psycopg
        cls.database = "snapshot_test_" + uuid.uuid4().hex[:12]
        cls.admin_dsn = os.environ.get("SNAPSHOT_TEST_ADMIN_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
        cls.dsn = psycopg.conninfo.make_conninfo(cls.admin_dsn, dbname=cls.database)
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        schema = subprocess.check_output(["docker", "exec", os.environ.get("SNAPSHOT_TEST_PG_CONTAINER", "datalaker-demo-source_pg-1"),
            "pg_dump", "-U", "postgres", "-d", "steward", "--schema-only", "--no-owner"], text=True)
        schema = "\n".join(line for line in schema.splitlines() if not line.startswith("\\"))
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute(schema)

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql
        with cls.psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database)))

    def setUp(self):
        self.env = patch.dict(os.environ, SNAPSHOT_RESTORE_DSN=self.dsn, DATASTEWARD_DSN=self.dsn)
        self.env.start()
        self.addCleanup(self.env.stop)
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("TRUNCATE role_assignment, source_contacts, runs, approvals, decisions, asset_catalog CASCADE")

    def apply(self, state):
        return restore.restore(state, baseline=BASELINE, origin=ORIGIN)

    def query(self, query, args=()):
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            return conn.execute(query, args).fetchall()

    def test_old_role_overlay_wins_new_bootstrap_and_is_reported(self):
        self.query("INSERT INTO role_assignment(role,person,valid_from,granted_by,reason)"
                   " VALUES ('steward','old@example.com',extract(epoch from now()),'admin','bootstrap') RETURNING id")
        result = self.apply({"roles": [{"role": "steward", "person": "new@example.com", "age_h": 720}]})
        from datasteward_gate.approvals import PgStore
        from identity import resolve_to
        agent_dsn = self.psycopg.conninfo.make_conninfo(self.dsn, user="agent_role", password="agent_pass")
        with PgStore(agent_dsn, readonly=True) as store:
            self.assertEqual(resolve_to(store, "steward"), "new@example.com")
        self.assertEqual(result["from_bundle"], BASELINE)
        self.assertEqual(result["overridden"][0]["before"][0]["person"], "old@example.com")
        self.assertEqual(result["overridden"][0]["after"][0]["person"], "new@example.com")

    def test_contacts_and_current_catalog_replace_existing_values(self):
        self.apply({"contacts": [{"source_id": "x", "email": "dba@example.com", "display_name": "Old"}],
                    "catalog": [{"asset": "x.orders", "kind": "link", "key": "customer", "value": "old"}]})
        result = self.apply({"contacts": [{"source_id": "x", "email": "dba@example.com", "display_name": "New"}],
                    "catalog": [{"asset": "x.orders", "kind": "link", "key": "customer", "value": "new", "status": "confirmed"}]})
        self.assertTrue(all(c["ok"] for c in result["checks"]))
        self.assertEqual(self.query("SELECT value FROM asset_catalog WHERE superseded_by IS NULL"), [("new",)])
        self.assertEqual(self.query("SELECT display_name FROM source_contacts"), [("New",)])

    def test_approval_decision_and_waiting_run_reach_production_resume_reader(self):
        result = self.apply({"runs": [{"id": "pending-copy", "kind": "ingest_table", "resumed": 2,
                            "checkpoint": {"batch": 8}, "next_action_h": -1}],
            "tickets": [{"id": "approval", "run": "pending-copy", "tool": "ingest_table",
                "args": {"source": "x", "table": "orders"}, "waits": True, "decision": "approve"}]})
        import runs
        with patch.object(runs, "_store", side_effect=lambda **kwargs: __import__('datasteward_gate.approvals', fromlist=['PgStore']).PgStore(self.dsn)):
            ready = runs.resumable()
        self.assertEqual(ready[0]["run_id"], "pending-copy")
        self.assertEqual(ready[0]["resumed"], 2)
        self.assertEqual(self.query("SELECT waiting_on FROM runs")[0][0], result["ids"]["ticket:approval"])

    def test_expect_failure_rolls_back_other_changes(self):
        self.apply({"roles": [{"role": "steward", "person": "old@example.com"}],
                    "runs": [{"id": "r", "kind": "copy"}]})
        with self.assertRaisesRegex(restore.RestoreFailed, "expect"):
            self.apply({"roles": [{"role": "steward", "person": "wrong@example.com"}],
                "patches": [{"table": "runs", "key": {"run_id": "r"}, "expect": {"status": "done"},
                             "set": {"status": "failed"}}]})
        self.assertEqual(self.query("SELECT person FROM role_assignment"), [("old@example.com",)])

    def test_single_condition_patch_reports_before_after_and_relative_time(self):
        self.apply({"runs": [{"id": "r", "kind": "copy"}]})
        now = time.time()
        result = self.apply({"patches": [{"table": "runs", "key": {"run_id": "r"},
            "expect": {"next_action_at": None}, "set": {"next_action_at": {"hours_from_restore": 2}}}]})
        row = self.query("SELECT next_action_at FROM runs")[0][0]
        self.assertAlmostEqual(row, now + 7200, delta=2)
        self.assertIsNone(result["overridden"][0]["before"][0]["next_action_at"])


@unittest.skipUnless(ENABLED, "set SNAPSHOT_LIVE_TESTS=1 for isolated real services")
class GreenMailRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "snapshot-mail-test-" + uuid.uuid4().hex[:10]
        subprocess.run(["docker", "run", "-d", "--name", cls.name,
            "-p", "127.0.0.1::3143", "-p", "127.0.0.1::8080",
            "-e", "GREENMAIL_OPTS=-Dgreenmail.setup.test.imap -Dgreenmail.setup.api -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled",
            "-e", "JAVA_OPTS=-Xmx128m", "greenmail/standalone:2.1.0"], check=True, capture_output=True)
        cls.wait()

    @classmethod
    def ports(cls) -> bool:
        """`docker run -d` 返回时端口映射还没填充，inspect 会拿到空列表。

        原先在 setUpClass 里紧接着 run 就读一次，必然撞 IndexError ——
        所以端口和服务就绪要一起轮询，不能先读一次再等。
        """
        data = json.loads(subprocess.check_output(["docker", "inspect", cls.name]))[0]
        mapped = data["NetworkSettings"]["Ports"]
        try:
            cls.imap_port = int(mapped["3143/tcp"][0]["HostPort"])
            cls.api_port = int(mapped["8080/tcp"][0]["HostPort"])
            return True
        except (KeyError, IndexError, TypeError):
            return False

    @classmethod
    def wait(cls):
        for _ in range(120):
            if cls.ports():
                try:
                    mailbox.users("127.0.0.1", cls.api_port)
                    return
                except OSError:
                    pass
            time.sleep(0.5)
        raise RuntimeError("isolated GreenMail not ready")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], check=True, capture_output=True)

    def test_all_accounts_mime_flags_dates_and_pending_mail_roundtrip(self):
        accounts = [{"address": "alias@example.com", "login": "alias-login"},
                    {"address": "unlisted@example.com", "login": "unlisted-login"}]
        mailbox.create_users("127.0.0.1", self.api_port, accounts)
        raw = b"From: a@example.com\r\nTo: alias@example.com\r\nMessage-ID: <history@example.com>\r\nSubject: old\r\n\r\nbody\r\n"
        with imaplib.IMAP4("127.0.0.1", self.imap_port) as conn:
            conn.login("alias-login", "ignored")
            self.assertEqual(conn.append("INBOX", "(\\Seen)", '"15-Sep-2026 10:00:00 +0000"', raw)[0], "OK")
            self.assertEqual(conn.append("INBOX", "()", '"15-Sep-2026 11:00:00 +0000"', raw.replace(b"<history", b"<pending"))[0], "OK")
        all_accounts = mailbox.users("127.0.0.1", self.api_port)
        self.assertEqual(len(all_accounts), 2)
        state = mailbox.capture("127.0.0.1", self.imap_port, all_accounts)
        subprocess.run(["docker", "restart", self.name], check=True, capture_output=True)
        self.wait()
        mailbox.create_users("127.0.0.1", self.api_port, state["accounts"])
        report = mailbox.restore("127.0.0.1", self.imap_port, state)
        self.assertEqual(report, {"accounts": 2, "messages": 2})
        with imaplib.IMAP4("127.0.0.1", self.imap_port) as conn:
            conn.login("alias-login", "ignored")
            conn.select("INBOX")
            self.assertEqual(len(conn.uid("search", None, "UNSEEN")[1][0].split()), 1)


if __name__ == "__main__":
    unittest.main()
