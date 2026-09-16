"""Runner contracts: complete baseline, reviewable overlays, output evidence.

These tests use real files/SQLite and stub only Docker/process boundaries.
Postgres/IMAP integration lives in test_snapshot_runner_live.py.
"""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.snapshot_runner import bundle, trajectory, input_audit, restore
from services.snapshot_runner.docker_backend import DockerBackend, SERVICES

spec = importlib.util.spec_from_file_location("runner_entrypoint", ROOT / "docker/agent-entrypoint.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def archive(path, entries):
    with tarfile.open(path, "w") as out:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mode = 0o600
            out.addfile(member, io.BytesIO(value))


def checkpoint(root):
    root.mkdir()
    for part in bundle.ARCHIVES:
        archive(root / f"{part}.tar", {"state": (part + "-original").encode()})
    bundle.write_json(root / "mail.json", {"accounts": []})
    bundle.write_json(root / "deployment.json", {
        s: {"image": "sha256:fixture", "environment": {}} for s in SERVICES})
    return bundle.seal(root, captured_at=123, metadata={"adapter": "datalaker-compose-v1",
        "project": "snapshot-test", "origin": "snapshot:real-run/before-reply"})


class SnapshotRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.audit = self.root / "audit"
        self.env = patch.dict(os.environ, HERMES_HOME=str(self.home),
            DATASTEWARD_INPUT_AUDIT_DIR=str(self.audit), DATASTEWARD_AUDIT_RUN_ID="run-1")
        self.env.start()
        self.addCleanup(self.env.stop)

    def db(self):
        conn = sqlite3.connect(self.home / "state.db")
        conn.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY,source TEXT,session_key TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
                tool_calls TEXT,tool_name TEXT,tool_call_id TEXT,timestamp REAL);
            INSERT INTO sessions VALUES ('one','email','thread-one'),('two','cron','thread-two');""")
        return conn

    @staticmethod
    def message(conn, session, role, text, calls=None, call_id=None):
        conn.execute("INSERT INTO messages (session_id,role,content,tool_calls,tool_call_id,timestamp) VALUES (?,?,?,?,?,1)",
                     (session, role, text, json.dumps(calls) if calls else None, call_id))
        conn.commit()

    def test_bundle_requires_every_plane_and_checks_every_byte(self):
        root = self.root / "snapshot"
        checkpoint(root)
        self.assertEqual(set(bundle.validate(root)["artifacts"]), bundle.PARTS)
        (root / "runtime.tar").write_bytes(b"changed")
        with self.assertRaisesRegex(bundle.SnapshotError, "changed|checksum"):
            bundle.validate(root)

    def test_unknown_manifest_field_and_absent_artifact_fail_before_stop(self):
        root = self.root / "snapshot"
        manifest = checkpoint(root)
        manifest["session"] = {}
        bundle.write_json(root / "manifest.json", manifest)
        backend = DockerBackend("snapshot-test")
        with patch.object(backend, "stop") as stop:
            with self.assertRaises(bundle.SnapshotError):
                backend.restore(root)
            stop.assert_not_called()

    def test_archive_path_escape_and_symlink_are_rejected(self):
        path = self.root / "bad.tar"
        archive(path, {"../../escape": b"bad"})
        with self.assertRaises(bundle.SnapshotError):
            bundle.inventory(path)
        with tarfile.open(path, "w") as out:
            member = tarfile.TarInfo("memory")
            member.type, member.linkname = tarfile.SYMTYPE, "/private"
            out.addfile(member)
        with self.assertRaises(bundle.SnapshotError):
            bundle.inventory(path)

    def test_pure_state_is_refused_before_connect(self):
        with patch.object(restore, "_connect") as connect:
            with self.assertRaisesRegex(restore.RestoreFailed, "基线"):
                restore.restore({"roles": [{"role": "steward", "person": "alice@example.com"}]})
            connect.assert_not_called()

    def test_explicit_empty_baseline_must_name_all_empty_planes(self):
        with patch.object(restore, "_connect") as connect:
            with self.assertRaisesRegex(restore.RestoreFailed, "empty"):
                restore.restore({}, baseline={"kind": "empty", "origin": "empty:first", "empty_planes": []})
            connect.assert_not_called()

    def test_overlay_origin_must_match_bundle_before_wipe(self):
        root = self.root / "snapshot"
        checkpoint(root)
        backend = DockerBackend("snapshot-test")
        with patch.object(backend, "restore") as apply:
            with self.assertRaisesRegex(bundle.SnapshotError, "origin"):
                backend.run(root, self.root / "out", state={"origin": "derived:wrong/base", "state": {}})
            apply.assert_not_called()

    def test_patch_requires_before_value_and_no_ambiguous_replacement(self):
        invalid = {"patches": [{"table": "runs", "key": {"run_id": "x"}, "set": {"status": "done"}, "expect": {"kind": "copy"}}]}
        with self.assertRaisesRegex(restore.RestoreFailed, "expect"):
            restore.check_state(invalid)
        valid = copy.deepcopy(invalid)
        valid["patches"][0]["expect"] = {"status": "waiting_human"}
        restore.check_state(valid)
        valid["runs"] = [{"id": "x", "kind": "copy"}]
        with self.assertRaisesRegex(restore.RestoreFailed, "同一张表"):
            restore.check_state(valid)

    def test_time_offsets_share_one_anchor(self):
        token = restore._ANCHOR.set(1000)
        try:
            self.assertEqual(restore._ahead(2) - restore._ago(3), 5 * 3600)
        finally:
            restore._ANCHOR.reset(token)
        with self.assertRaises(restore.RestoreFailed):
            restore.check_state({"runs": [{"id": "x", "kind": "copy", "age_h": float("nan")}]})

    def test_restored_messages_are_context_not_new_trajectory(self):
        with self.db() as conn:
            self.message(conn, "one", "assistant", "old answer")
            trajectory.baseline("run-1")
            self.message(conn, "one", "assistant", "new final answer")
        result = trajectory.collect(run_id="run-1")
        self.assertEqual([m["content"] for m in result["messages"]], ["new final answer"])
        self.assertEqual(result["messages"][0]["session_id"], "one")
        self.assertEqual(result["final_assistant"]["content"], "new final answer")

    def test_same_tool_id_in_other_session_cannot_complete_a_call(self):
        with self.db() as conn:
            trajectory.baseline("run-1")
            self.message(conn, "one", "assistant", "", [{"id": "shared", "function": {"name": "read"}}])
            self.message(conn, "two", "tool", "different session result", call_id="shared")
        result = trajectory.collect(run_id="run-1")
        self.assertEqual(result["interrupted_calls"][0]["session_id"], "one")

    def test_last_output_is_observed_directly_without_another_request(self):
        trajectory.baseline("run-1")
        long_output = "final response " * 2500
        input_audit.on_post_api_request(api_request_id="api1", session_id="one",
            assistant_message={"role": "assistant", "content": long_output, "tool_calls": []})
        result = trajectory.collect(run_id="run-1")
        self.assertEqual(result["final_assistant"]["content"], long_output)
        self.assertEqual(result["counts"]["responses"], 1)

    def test_inputs_alone_never_become_outputs(self):
        trajectory.baseline("run-1")
        input_audit.on_pre_api_request(request_messages=[{"role": "assistant", "content": "old output in input"}],
                                      api_request_id="pending", session_id="one")
        result = trajectory.collect(run_id="run-1")
        self.assertIsNone(result["final_assistant"])
        self.assertEqual(result["interrupted_requests"], [{"session_id": "one", "api_request_id": "pending"}])

    def test_missing_baseline_or_failed_observer_is_not_complete(self):
        self.assertEqual(trajectory.collect(run_id="run-1")["state"], "inconclusive")
        trajectory.baseline("run-1")
        (self.audit / "run-1/audit_error.json").write_text("{}")
        self.assertEqual(trajectory.collect(run_id="run-1")["state"], "inconclusive")

    def test_finish_stops_before_export_and_completion(self):
        events = []
        process = Mock(returncode=0)
        with patch.dict(os.environ, DEMO_CONTROL_DIR="/control"), patch.object(entry, "CURRENT_RUN", "run-1"), \
             patch.object(entry, "stop_gateway", side_effect=lambda p: events.append("stop")), \
             patch.object(entry, "archive_requests", side_effect=lambda: events.append("requests")), \
             patch.object(entry, "archive_trajectory", side_effect=lambda: events.append("trajectory")), \
             patch.object(entry, "record_completion", side_effect=lambda *args: events.append("completion")):
            entry.finish_run("window_closed", process)
        self.assertEqual(events, ["stop", "requests", "trajectory", "completion"])

    def test_verified_runtime_is_not_cleared_on_reset(self):
        control = self.root / "control"
        bundle.write_json(control / "restored/run-1.json", {"run_id": "run-1", "snapshot_sha256": "abc"})
        with patch.object(entry, "CONTROL_DIR", control), patch.object(entry, "clear_runtime") as clear, \
             patch.object(entry, "clear_turn_signal"), patch.object(entry, "acknowledge"):
            entry.accept_reset(Path("request"), "run-1")
            clear.assert_not_called()
            self.assertTrue(entry.RESTORED_RUNTIME)

    def test_module_imports_without_services_on_pythonpath(self):
        code = "import sys; sys.path.insert(0,sys.argv[1]); from services.snapshot_runner import evidence,trajectory; from services.snapshot_runner.restore import _fingerprint; assert _fingerprint('x',{})"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
