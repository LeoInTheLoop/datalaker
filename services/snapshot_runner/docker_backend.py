"""Cold checkpoint adapter for the isolated DataLaker Docker Compose stack.

No Docker socket in the agent/UI. The operator-side CLI owns orchestration.
All writable state is captured, including Postgres roles/secrets/source data,
MinIO objects and the REST catalog's SQLite database (not just lake table names).
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from . import bundle, mailbox
from .input_audit import scrub

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("source_pg", "minio", "iceberg_rest", "trino", "greenmail", "steward-agent", "approval-callback", "demo-ui")
LOCATIONS = {"postgres": ("source_pg", "/var/lib/postgresql/data"),
             "objects": ("minio", "/data"), "runtime": ("steward-agent", "/state/hermes"),
             "iceberg": ("iceberg_rest", "/tmp")}
# The REST fixture keeps its catalog in the container layer, outside any volume.
CATALOG_FILES = {"iceberg_catalog.db", "iceberg_catalog.db-wal", "iceberg_catalog.db-shm"}


class DockerBackend:
    def __init__(self, project="datalaker-demo", root=ROOT):
        if not re.fullmatch(r"(?:datalaker-demo|snapshot-[a-z0-9_-]+)", project):
            raise bundle.SnapshotError("runner only accepts datalaker-demo or snapshot-* isolated projects")
        self.project, self.root = project, Path(root)
        self.compose = ["docker", "compose", "--project-name", project,
            "--project-directory", str(self.root / "infra"), "--env-file", str(self.root / ".env"),
            "-f", str(self.root / "infra/docker-compose.yml"),
            "-f", str(self.root / "infra/docker-compose.demo.yml"), "--profile", "demo"]
        self.containers = {}

    @staticmethod
    def command(args, *, stdin=None, output=None, timeout=180):
        result = subprocess.run(args, input=stdin, stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise bundle.SnapshotError(f"{args[0]} failed ({result.returncode}): "
                                       + scrub(result.stderr.decode(errors="replace"))[-2000:])
        return result.stdout if output is None else b""

    @contextmanager
    def lock(self):
        with open(Path(tempfile.gettempdir()) / f"{self.project}-snapshot.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise bundle.SnapshotError("another snapshot operation owns this stack") from exc
            yield

    def discover(self):
        ids = self.command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"]).decode().split()
        if not ids:
            raise bundle.SnapshotError("stack does not exist; use the provision command first")
        found = json.loads(self.command(["docker", "inspect", *ids]))
        self.containers = {item["Config"]["Labels"].get("com.docker.compose.service"): item for item in found
                           if item["Config"]["Labels"].get("com.docker.compose.oneoff", "False").lower() != "true"}
        missing = set(SERVICES) - self.containers.keys()
        if missing:
            raise bundle.SnapshotError("missing stack services: " + ", ".join(sorted(missing)))
        if "greenmail/" not in self.containers["greenmail"]["Config"]["Image"]:
            raise bundle.SnapshotError("mail destination must be isolated GreenMail")
        if "-Dgreenmail.auth.disabled" not in self.environment("greenmail").get("GREENMAIL_OPTS", ""):
            raise bundle.SnapshotError("mail adapter requires GreenMail's isolated auth-disabled mode")
        if self.environment("steward-agent").get("DEMO_CONTROL_DIR") != "/control":
            raise bundle.SnapshotError("agent is not configured for isolated runs")
        for part in ("postgres", "objects", "runtime"):
            self.volume(part)
        return self.containers

    def provision(self):
        # Only creates the isolated project. Gateway waits for a prepared run.
        self.command([*self.compose, "up", "-d", *SERVICES], timeout=600)
        self.discover()
        self.wait_dependencies()

    def environment(self, service):
        return dict(item.split("=", 1) for item in self.containers[service]["Config"]["Env"] if "=" in item)

    def cid(self, service):
        return self.containers[service]["Id"]

    def volume(self, part):
        service, path = LOCATIONS[part]
        mount = next((m for m in self.containers[service]["Mounts"] if m["Destination"] == path), None)
        if not mount or mount["Type"] != "volume":
            raise bundle.SnapshotError(f"{part} is not an isolated named volume")
        data = json.loads(self.command(["docker", "volume", "inspect", mount["Name"]]))[0]
        if (data.get("Labels") or {}).get("com.docker.compose.project") != self.project:
            raise bundle.SnapshotError(f"{part} volume does not belong to this project")
        return mount["Name"]

    def stop(self, *services):
        self.command(["docker", "stop", "--time", "40", *(self.cid(s) for s in services)], timeout=240)

    def start(self, *services):
        self.command(["docker", "start", *(self.cid(s) for s in services)])

    def port(self, service, port):
        bindings = self.containers[service]["HostConfig"]["PortBindings"].get(f"{port}/tcp") or []
        if len(bindings) != 1 or bindings[0].get("HostIp") not in ("127.0.0.1", "::1"):
            raise bundle.SnapshotError(f"{service}:{port} must have one loopback binding")
        return int(bindings[0]["HostPort"])

    def _copy_out(self, service, path, target):
        with target.open("wb") as out:
            os.chmod(target, 0o600)
            self.command(["docker", "cp", f"{self.cid(service)}:{path}/.", "-"], output=out)

    def _archive(self, part, target):
        service, path = LOCATIONS[part]
        if part != "iceberg":
            self._copy_out(service, path, target)
            return
        import tarfile
        # Copy only the catalog files; /tmp also has native libraries and PID files.
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "tmp.tar"
            self._copy_out(service, path, raw)
            with tarfile.open(raw) as source, tarfile.open(target, "w") as dest:
                found = False
                for member in source:
                    name = member.name.removeprefix("./")
                    if name in CATALOG_FILES:
                        member.name = name
                        dest.addfile(member, source.extractfile(member))
                        found = found or name == "iceberg_catalog.db"
                if not found:
                    raise bundle.SnapshotError("REST catalog database missing; lake snapshot would be incomplete")
        target.chmod(0o600)

    def _deployment(self):
        return {service: {"image": self.containers[service]["Image"],
                          "environment": self.environment(service)} for service in SERVICES}

    def capture(self, destination):
        destination = Path(destination).resolve()
        self.discover()
        mailbox.users("127.0.0.1", self.port("greenmail", 8080))
        bundle.create_directory(destination)
        # UI/callback cannot keep modifying state during capture or restore.
        current = self.read_agent_json("/control/active_run.json")
        self.stop("demo-ui", "approval-callback", "steward-agent", "trino")
        try:
            accounts = mailbox.users("127.0.0.1", self.port("greenmail", 8080))
            mail = mailbox.capture("127.0.0.1", self.port("greenmail", 3143), accounts)
            self.stop("source_pg", "iceberg_rest", "minio", "greenmail")
            captured_at = time.time()
            for part in bundle.ARCHIVES:
                self._archive(part, destination / f"{part}.tar")
            bundle.write_json(destination / "mail.json", mail)
            bundle.write_json(destination / "deployment.json", self._deployment())
            bundle.seal(destination, captured_at=captured_at,
                        metadata={"adapter": "datalaker-compose-v1", "project": self.project,
                                  "origin": f"snapshot:{current.get('run_id') or 'environment'}/{int(captured_at)}"})
            return {"state": "captured", "snapshot": str(destination), "captured_at": captured_at}
        except BaseException:
            # Incomplete directories deliberately have no valid manifest.
            self.stop("source_pg", "iceberg_rest", "minio", "greenmail")
            raise

    def compatible(self, deployment):
        if set(deployment) != set(SERVICES):
            raise bundle.SnapshotError("invalid deployment inventory")
        for service in SERVICES:
            if service in ("source_pg", "minio", "iceberg_rest", "greenmail"):
                if deployment[service]["image"] != self.containers[service]["Image"]:
                    raise bundle.SnapshotError(f"physical checkpoint image mismatch: {service}")
            # Signing keys/addresses/connection coordinates are state dependencies.
            # Model/code/budget may change intentionally in a comparative run.
            now, before = self.environment(service), deployment[service]["environment"]
            keys = {k for k in now.keys() | before.keys() if k.startswith(("DATASTEWARD_", "SOURCE_", "NORTHWIND_", "EMAIL_", "MAIL_", "SMTP_", "AWS_", "CATALOG_", "POSTGRES_"))}
            keys -= {"DATASTEWARD_INPUT_AUDIT_DIR", "EMAIL_POLL_INTERVAL"}
            different = [k for k in keys if before.get(k) != now.get(k)]
            if different:
                raise bundle.SnapshotError(f"state-dependent configuration changed: {service}: " + ", ".join(sorted(different)))

    def _restore_archive(self, part, source, expected):
        service, path = LOCATIONS[part]
        if part == "iceberg":
            # No mount to clear. A helper container shares the stopped container's
            # writable layer only via docker cp: overwrite database and sidecars.
            # Every cold SQLite DB is checkpointed before capture; stale sidecars
            # must be absent in both source and destination (checked below).
            with tempfile.TemporaryDirectory() as tmp:
                before = Path(tmp) / "before.tar"
                self._archive(part, before)
                if any(name.endswith(("-wal", "-shm")) for name in bundle.inventory(before)):
                    raise bundle.SnapshotError("REST catalog did not close cleanly; refusing stale SQLite sidecars")
        else:
            code = "import pathlib,shutil; p=pathlib.Path('/restore'); [(shutil.rmtree(c) if c.is_dir() and not c.is_symlink() else c.unlink()) for c in p.iterdir()]"
            self.command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                          "--mount", f"type=volume,source={self.volume(part)},target=/restore",
                          self.containers["steward-agent"]["Image"], "-c", code])
        with source.open("rb") as stream:
            result = subprocess.run(["docker", "cp", "-a", "-", f"{self.cid(service)}:{path}"],
                                    stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if result.returncode:
            raise bundle.SnapshotError(f"restore copy failed: {part}: " + scrub(result.stderr.decode())[-1000:])
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "verify.tar"
            self._archive(part, copied)
            if bundle.inventory(copied) != expected:
                raise bundle.SnapshotError(f"restored file content/ownership mismatch: {part}")

    def wait_dependencies(self, seconds=120):
        deadline = time.monotonic() + seconds
        import socket
        while time.monotonic() < deadline:
            inspected = json.loads(self.command(["docker", "inspect", self.cid("source_pg"), self.cid("trino")]))
            ready = all(c["State"]["Running"] and c["State"].get("Health", {}).get("Status") == "healthy" for c in inspected)
            try:
                with socket.create_connection(("127.0.0.1", self.port("greenmail", 3143)), timeout=2):
                    if ready:
                        return
            except OSError:
                pass
            time.sleep(1)
        raise bundle.SnapshotError("database/lake/mail readiness timeout")

    def restore(self, source):
        source = Path(source).resolve()
        manifest = bundle.validate(source)  # BEFORE quiescing/wiping anything
        self.discover()
        self.compatible(json.loads((source / "deployment.json").read_text()))
        self.stop("demo-ui", "approval-callback", "steward-agent", "trino", "source_pg", "iceberg_rest", "minio", "greenmail")
        for part in sorted(bundle.ARCHIVES):
            self._restore_archive(part, source / f"{part}.tar", manifest["artifacts"][part]["inventory"])
        self.start("source_pg", "minio", "iceberg_rest", "greenmail", "trino")
        self.wait_dependencies()
        mail_state = json.loads((source / "mail.json").read_text())
        mailbox.create_users("127.0.0.1", self.port("greenmail", 8080), mail_state["accounts"])
        mail_check = mailbox.restore("127.0.0.1", self.port("greenmail", 3143), mail_state)
        return {"state": "restored", "snapshot_sha256": bundle.sha256(source / "manifest.json"),
                "captured_at": manifest["captured_at"], "restored_at": time.time(),
                "clock": manifest["clock"], "checked": sorted(bundle.PARTS), "mail": mail_check}

    def clear_control(self):
        volume = next(m["Name"] for m in self.containers["steward-agent"]["Mounts"] if m["Destination"] == "/control")
        code = "import pathlib,shutil; p=pathlib.Path('/control'); [(shutil.rmtree(c) if c.is_dir() and not c.is_symlink() else c.unlink()) for c in p.iterdir()]"
        self.command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                      "--mount", f"type=volume,source={volume},target=/control",
                      self.containers["steward-agent"]["Image"], "-c", code])

    def put_control(self, relative, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(relative).name
            bundle.write_json(path, value)
            # Nested control directories are created by a helper sharing only this volume.
            volume = next(m["Name"] for m in self.containers["steward-agent"]["Mounts"] if m["Destination"] == "/control")
            code = "import pathlib,sys; p=pathlib.Path('/control')/sys.argv[1]; p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix('.tmp'); t.write_bytes(sys.stdin.buffer.read()); t.replace(p)"
            self.command(["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "python",
                "--mount", f"type=volume,source={volume},target=/control",
                self.containers["steward-agent"]["Image"], "-c", code, relative], stdin=path.read_bytes())

    def run(self, source, output, *, seconds=60, state=None, mail=None):
        if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or not 0 < seconds <= 86400:
            raise bundle.SnapshotError("window must be between 0 and 86400 seconds")
        if state is not None:
            from .restore import check_state
            if set(state) != {"origin", "state"} or not str(state["origin"]).startswith("derived:"):
                raise bundle.SnapshotError("overlay must contain origin: derived:<baseline>/<change> and state")
            check_state(state["state"])
        if mail is not None and (set(mail) != {"from", "subject", "body"} or not all(isinstance(v, str) for v in mail.values())):
            raise bundle.SnapshotError("current mail requires only from/subject/body strings")
        manifest = bundle.validate(Path(source))
        origin = manifest["metadata"]["origin"]
        if state is not None and not state["origin"].startswith(f"derived:{origin}/"):
            raise bundle.SnapshotError("overlay origin does not reference this bundle")
        output = Path(output).resolve()
        bundle.create_directory(output)
        run_id = "snapshot-" + uuid.uuid4().hex
        result = {"run_id": run_id, "state": "preparing"}
        released = False
        try:
            result["restore"] = self.restore(source)
            result["from_bundle"] = {"origin": origin, "sha256": result["restore"]["snapshot_sha256"]}
            if state is not None:
                request = {"baseline": {"kind": "bundle", **result["from_bundle"]}, **state}
                raw = self.command([*self.compose, "run", "--rm", "-T", "--no-deps", "demo-ui",
                    "python", "/app/services/snapshot_runner/restore.py", "-"], stdin=json.dumps(request).encode())
                result["overlay"] = json.loads(raw)
                if not result["overlay"].get("ok"):
                    raise bundle.SnapshotError("state overlay failed")
            self.clear_control()
            self.put_control(f"restored/{run_id}.json", {"run_id": run_id, "snapshot_sha256": result["restore"]["snapshot_sha256"]})
            self.put_control(f"requests/{run_id}.json", {"run_id": run_id, "requested_at": time.time()})
            # Old reset/prepared files cannot replay: entrypoint only releases this unique id.
            released = True
            self.start("steward-agent")
            deadline = time.time() + seconds
            self.put_control(f"prepared/{run_id}.json", {"run_id": run_id, "deadline": deadline})
            self.start("approval-callback")
            delivered = mail is None
            while time.time() < deadline + 50:
                completion = self.read_agent_json(f"/audit/{run_id}/completion.json")
                if completion:
                    success = completion.get("gateway_ready") and completion.get("reason") in ("window_closed", "turn_limit", "external_stop")
                    result.update(state="stopped" if success else "failed", completion=completion)
                    break
                if not delivered:
                    status = self.read_agent_json("/status/status.json")
                    if status.get("run_id") == run_id and status.get("state") == "ready":
                        self.send_mail(mail)
                        delivered = True
                time.sleep(1)
            else:
                raise bundle.SnapshotError("runner supervisor did not acknowledge stop")
            if not delivered:
                result["stimulus_error"] = "gateway stopped before current mail could be delivered"
                result["state"] = "failed"
        except BaseException as exc:
            result.update(state="failed", error=scrub(f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            try:
                if released:
                    self.stop("steward-agent", "approval-callback")
                    result["export"] = self.export(run_id, output)
                    if result["export"]["state"] != "recorded":
                        result["state"] = "failed"
            finally:
                bundle.write_json(output / "run.json", result)
        return result

    def read_agent_json(self, path):
        result = subprocess.run(["docker", "exec", self.cid("steward-agent"), "cat", path],
                                capture_output=True, timeout=10)
        if result.returncode:
            return {}
        return json.loads(result.stdout)

    def export(self, run_id, output):
        target = Path(output) / "audit"
        target.mkdir(mode=0o700, exist_ok=True)
        result = subprocess.run(["docker", "cp", f"{self.cid('steward-agent')}:/audit/{run_id}/.", str(target)],
                                capture_output=True, timeout=60)
        if result.returncode:
            return {"state": "inconclusive", "reason": "run audit directory unavailable"}
        trajectory = target / "trajectory.json"
        if not trajectory.exists():
            return {"state": "inconclusive", "reason": "trajectory not archived"}
        return {"state": json.loads(trajectory.read_text()).get("state"), "directory": str(target)}

    def send_mail(self, value):
        import smtplib
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid
        message = EmailMessage()
        message["From"] = value["from"]
        message["To"] = self.environment("steward-agent").get("EMAIL_ADDRESS", "claw@acme.test")
        message["Subject"] = value["subject"]
        message["Date"], message["Message-ID"] = formatdate(), make_msgid(domain="snapshot.local")
        domain = value["from"].rsplit("@", 1)[-1]
        message["Authentication-Results"] = f"mx.acme-sim.test; dmarc=pass header.from={domain}"
        message.set_content(value["body"])
        with smtplib.SMTP("127.0.0.1", self.port("greenmail", 3025), timeout=10) as smtp:
            smtp.send_message(message)
