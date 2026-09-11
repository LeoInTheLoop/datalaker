#!/usr/bin/env python3
"""One-page observer for a real Hermes / data-steward rehearsal.

This is deliberately a thin adapter.  It never imports a data-steward tool,
never writes a decision, and never chooses a model action.  Its only writes
are human inbound mail, isolated demo snapshot reset requests, and its own
read-only run history.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import json
import os
import pathlib
import re
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

HOST = os.environ.get("MAILSIM_HOST", "greenmail")
SMTP_PORT = int(os.environ.get("MAILSIM_SMTP_PORT", "3025"))
IMAP_PORT = int(os.environ.get("MAILSIM_IMAP_PORT", "3143"))
PORT = int(os.environ.get("DEMO_UI_PORT", "8088"))
CLAW = "claw@acme.test"
PEOPLE = ("boss@acme.com", "wang@acme.com", "dba@acme.com")
STATUS_FILE = pathlib.Path(os.environ.get("DEMO_AGENT_STATUS_DIR", "/status")) / "status.json"
CONTROL_DIR = pathlib.Path(os.environ.get("DEMO_CONTROL_DIR", "/control"))
RUN_DIR = pathlib.Path(os.environ.get("DEMO_RUN_DIR", "/runs"))
CASES_FILE = pathlib.Path(os.environ.get("DEMO_CASES_FILE", ROOT / "demo" / "cases.json"))
APPROVAL_INTERNAL_URL = os.environ.get(
    "APPROVAL_INTERNAL_URL", "http://approval-callback:8787").rstrip("/")
MAIL_LINK_RE = re.compile(
    r"https?://127\.0\.0\.1:\d+/(approve|deny|choose)\?t=([\w.\-]+)")
APPROVAL_URL_RE = re.compile(
    r"https?://[^/\s]+/(?:approve|deny|choose)\?t=[\w.\-]+")
DSN_RE = re.compile(r"postgres(?:ql)?://[^\s'\"<>]+", re.IGNORECASE)
MAX_OBSERVATIONS = 120


def redact(text: str) -> str:
    """Credentials and one-time approval tokens never leave the adapter."""
    value = str(text or "")
    value = APPROVAL_URL_RE.sub("[审批链接已隐藏]", value)
    return DSN_RE.sub("[数据库连接串已隐藏]", value)


def body_of(msg) -> str:
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_content()
            return ""
        return msg.get_content()
    except Exception:  # noqa: BLE001
        return ""


def subject_of(msg) -> str:
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(msg.get("Subject") or "")))
    except Exception:  # noqa: BLE001
        return msg.get("Subject") or ""


def _link_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def fetch(box: str, limit: int = 80) -> list[dict]:
    """Read a real GreenMail inbox.  The token itself never reaches the browser."""
    out = []
    im = imaplib.IMAP4(HOST, IMAP_PORT, timeout=8)
    try:
        im.login(box, box)
        im.select("INBOX", readonly=True)
        _, ids = im.search(None, "ALL")
        for uid in (ids[0].split() if ids and ids[0] else [])[-limit:]:
            # The observer must not mark new mail as \Seen. Hermes consumes
            # unread messages from the same GreenMail inbox; a normal RFC822
            # fetch here could race the gateway and make a user reply vanish
            # before Hermes sees it.
            _, raw = im.fetch(uid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            raw_text = body_of(msg)
            sender = msg.get("From", "")
            actor = msg.get("X-Demo-Actor")
            if actor:
                origin = "演练人员输入"
            elif CLAW in sender and (subject_of(msg).startswith("[数据管家]")
                                     or raw_text.startswith("⚠️ Gateway shutting down")):
                origin = "系统通知模板"
            elif CLAW in sender:
                origin = "模型邮件输出"
            else:
                origin = "外部邮件"
            links = []
            for action, token in MAIL_LINK_RE.findall(raw_text):
                url = f"http://127.0.0.1:0/{action}?t={token}"
                links.append({"id": _link_id(url), "action": action})
            out.append({
                "id": msg.get("Message-ID") or f"{box}:{uid.decode()}",
                "box": box, "from": sender, "to": msg.get("To", ""),
                "subject": redact(subject_of(msg)), "date": msg.get("Date", ""),
                "body": redact(raw_text)[:6000], "origin": origin, "links": links,
                "_links": [(link["id"], url) for link, (_, token) in
                           zip(links, MAIL_LINK_RE.findall(raw_text))
                           for url in [f"http://127.0.0.1:0/{link['action']}?t={token}"]],
            })
    finally:
        try:
            im.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def mailboxes() -> list[dict]:
    return [m for box in PEOPLE + (CLAW,) for m in fetch(box)]


def approval_index(messages: list[dict] | None = None) -> dict[str, str]:
    index = {}
    for message in messages if messages is not None else mailboxes():
        index.update(dict(message.get("_links", [])))
    return index


def public_mail(messages: list[dict]) -> list[dict]:
    return [{k: (redact(v) if k in ("subject", "body") else v)
             for k, v in m.items() if k != "_links"} for m in messages]


def db_rows(sql: str, params=()):
    import psycopg
    with psycopg.connect(os.environ["DASHBOARD_DSN"], connect_timeout=5) as conn:
        return conn.execute(sql, params).fetchall()


def admin_rows(sql: str, params=()):
    import psycopg
    with psycopg.connect(os.environ["DEMO_ADMIN_DSN"], connect_timeout=10,
                         autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def admin_exec(sql: str, params=()) -> None:
    import psycopg
    with psycopg.connect(os.environ["DEMO_ADMIN_DSN"], connect_timeout=10,
                         autocommit=True) as conn:
        conn.execute(sql, params)


def source_rows(sql: str):
    import psycopg
    with psycopg.connect(os.environ["DEMO_SOURCE_DSN"], connect_timeout=5) as conn:
        return conn.execute(sql).fetchall()


def trino(sql: str):
    import sync
    return sync._trino(sql, timeout=30)


def safe(call, fallback):
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        return fallback(type(exc).__name__)


def agent_state() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"state": "starting", "detail": "", "run_id": ""}


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class RunStore:
    """Run history is a presentation record, outside the governance database."""
    def __init__(self, path: pathlib.Path):
        self.path = path / "runs.json"
        self.lock = threading.RLock()

    def read(self) -> dict:
        with self.lock:
            try:
                return self._clean(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                return {"current": None, "history": []}

    @staticmethod
    def _clean(value, key=""):
        """Scrub legacy observer records before they can be returned or re-written."""
        if isinstance(value, dict):
            return {k: RunStore._clean(v, k) for k, v in value.items()
                    if k != "_links"}
        if isinstance(value, list):
            items = [RunStore._clean(v, key) for v in value]
            if key == "observations":
                # Mail polling is not an event.  Keeping every snapshot made a
                # read-only refresh grow runs.json and retained old credentials.
                items = [v for v in items
                         if not (isinstance(v, dict) and v.get("kind") == "mail_read")]
                items = items[-MAX_OBSERVATIONS:]
            return items
        if isinstance(value, str):
            return redact(value)
        return value

    def write(self, value: dict) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._clean(value), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    def scrub_legacy(self) -> None:
        if self.path.exists():
            self.write(self.read())

    def current(self) -> dict | None:
        return self.read().get("current")

    def begin(self, case: dict, snapshot: dict) -> dict:
        data = self.read()
        prior = data.get("current")
        if prior:
            prior["ended_at"] = time.time()
            prior["end_reason"] = "superseded_by_new_run"
            data.setdefault("history", []).append(prior)
        run = {
            "id": f"demo-{uuid.uuid4().hex[:12]}", "case_id": case["id"],
            "case_title": case["title"], "snapshot_id": snapshot["id"],
            "snapshot_title": snapshot["title"], "database": snapshot["database"],
            "started_at": time.time(), "state": "reset_requested",
            "observations": [], "approval_attempts": [], "verification": [],
        }
        data["current"] = run
        self.write(data)
        return run

    def update_current(self, **fields) -> dict | None:
        data = self.read()
        if not data.get("current"):
            return None
        data["current"].update(fields)
        self.write(data)
        return data["current"]

    def append(self, field: str, item: dict) -> None:
        data = self.read()
        if not data.get("current"):
            return
        data["current"].setdefault(field, []).append(item)
        self.write(data)


RUNS = RunStore(RUN_DIR)
RUNS.scrub_legacy()


def cases() -> tuple[dict[str, dict], dict[str, dict]]:
    raw = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return ({item["id"]: item for item in raw["cases"]},
            {item["id"]: item for item in raw["snapshots"]})


def public_cases() -> list[dict]:
    """Return only the Snapshot Case state the browser may render.

    Staging fields such as ``steps`` and canned ``replies`` remain in the case
    source for a later full-simulation view, but are neither model input nor
    browser data in the Snapshot view.  The real Hermes turn receives its tool
    schemas from the runtime, not from this adapter.
    """
    case_map, _ = cases()
    visible = ("id", "title", "environment", "snapshots")
    return [{key: case[key] for key in visible if key in case}
            for case in case_map.values()]


def public_snapshot(snapshot: dict | None) -> dict | None:
    """Return browser-safe Snapshot state, excluding judge and gate metadata."""
    if not isinstance(snapshot, dict):
        return None
    visible = ("id", "database", "title", "source_tables", "table_scope", "note", "history", "opening")
    return {key: snapshot[key] for key in visible if key in snapshot}


def public_snapshots() -> list[dict]:
    """Expose only a selectable Snapshot's human state, never its judge input."""
    case_map, snapshot_map = cases()
    selectable = {snapshot_id for case in case_map.values()
                  for snapshot_id in case.get("snapshots", [])}
    return [public_snapshot(snapshot) for snapshot_id, snapshot in snapshot_map.items()
            if snapshot_id in selectable]


def active_snapshot() -> dict | None:
    current = RUNS.current()
    if not current:
        return None
    try:
        _, snapshots = cases()
        return snapshots.get(current["snapshot_id"])
    except Exception:  # noqa: BLE001
        return None


def lake_data() -> dict:
    out = {}
    for schema in ("bronze", "silver", "gold"):
        current = trino("SELECT table_name FROM iceberg.information_schema.tables "
                        f"WHERE table_schema='{schema}'")
        out[schema] = {}
        for row in current:
            table = row.split(",")[-1]
            out[schema][table] = int(trino(
                f"SELECT count(*) FROM iceberg.{schema}.{_quoted(table)}")[0])
    return out


def lake_columns_data(lake: dict | None = None) -> dict:
    """Return actual lake columns so gold checks cannot pass on table existence alone."""
    lake = lake or lake_data()
    if not all(schema in lake and isinstance(lake.get(schema), dict)
               for schema in ("bronze", "silver", "gold")):
        return {}
    out = {}
    for schema, tables in lake.items():
        out[schema] = {}
        for table in tables:
            rows = trino(
                "SELECT column_name FROM iceberg.information_schema.columns "
                f"WHERE table_schema='{schema}' AND table_name='{table}' "
                "ORDER BY ordinal_position")
            out[schema][table] = [r.strip().strip('"') for r in rows if r.strip()]
    return out


def _answer_row(row) -> tuple[str, ...]:
    """Canonicalize CSV/JSON rows for an exact model-vs-source comparison."""
    if isinstance(row, str):
        values = row.split(",")
    else:
        values = list(row or [])
    out = []
    for value in values:
        text = str(value).strip().strip('"').lower()
        try:
            from decimal import Decimal
            text = format(Decimal(text).normalize(), "f")
        except Exception:  # noqa: BLE001
            pass
        out.append(text)
    return tuple(out)


class DemoNotReady(RuntimeError):
    """The isolated Snapshot planes have not all become available yet."""


def snapshot_dependencies_ready() -> bool:
    """Check reset planes before making a run id, without changing either."""
    try:
        trino("SELECT 1")
        admin_rows("SELECT 1")
    except Exception:  # noqa: BLE001
        return False
    return True


def approval_summary(tool_name: str, raw_args) -> str:
    """Return a small, safe approval explanation; never return raw args/DSNs."""
    try:
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
    except (TypeError, json.JSONDecodeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    asset = str(args.get("asset") or args.get("table") or args.get("source_id") or "当前数据")
    asset = re.sub(r"[^A-Za-z0-9_.-]", "", asset)[:160] or "当前数据"
    if tool_name == "define_semantics":
        allowed = args.get("allowed_values")
        values = ([str(value)[:40] for value in allowed
                   if re.fullmatch(r"[A-Za-z0-9_. -]+", str(value))]
                  if isinstance(allowed, list) else [])
        if values:
            return (f"为 {asset} 登记发布口径：gold 只保留 {' / '.join(values)}，"
                    "原始值留在 silver。")
        return f"为 {asset} 登记业务口径。"
    if tool_name == "connect_source":
        return f"使用已提供的连接信息接入 {asset}。"
    if tool_name == "ingest_table":
        return f"把 {asset} 复制进 bronze 层。"
    if tool_name == "apply_cleaning_rule":
        return f"按已确认规则清洗 {asset}，并保留原始值。"
    if tool_name == "publish_gold":
        return f"将 {asset} 发布到 gold 层供业务使用。"
    return f"确认「{tool_name or '当前动作'}」的执行范围。"


def _result_check(name: str, state: str, evidence: str) -> dict:
    return {"name": name, "state": state, "evidence": evidence}


def _event_payload(event: dict) -> dict:
    try:
        value = json.loads(event.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def evaluate_case(case: dict | None, snapshot: dict | None = None, *, events: list[dict], lake: dict,
                  lake_columns: dict, silver: dict, provenance: list[dict],
                  answer_comparison: dict, messages: list[dict] | None = None,
                  contacts: list[dict] | None = None) -> dict:
    """Judge only the Case's declared terminal result from independent records.

    ``expected_outcome`` is intentionally absent from both public Case metadata
    and ``case_packet()``.  This evaluator sees recorded events/data after the
    model acted; it never supplies an answer or a tool sequence to the model.
    """
    expected = (snapshot or {}).get("expected_outcome") or (case or {}).get("expected_outcome") or {}
    if not expected:
        return {"state": "pending", "expected": "本 Case 尚未定义预期结果。", "checks": []}

    messages, contacts = messages or [], contacts or []
    kind = expected.get("kind")
    checks: list[dict] = []
    if kind == "sales_leader":
        target = _answer_row(expected.get("row") or ())
        model_rows = [_answer_row(row) for row in answer_comparison.get("model_rows", [])]
        if not model_rows:
            checks.append(_result_check("模型最终答案", "pending", "还没有记录到 answer_with_link 的结果。"))
        elif target in model_rows:
            checks.append(_result_check("模型最终答案", "pass", expected["answer"]))
        else:
            checks.append(_result_check("模型最终答案", "fail", "模型已给出结果，但不含预期第一名及数值。"))
        if not model_rows:
            checks.append(_result_check("逐行独立核对", "pending", "等待模型结果后与源库九行直算核对。"))
        elif answer_comparison.get("matched") and len(answer_comparison.get("source_rows", [])) == 9:
            checks.append(_result_check("逐行独立核对", "pass", "模型结果与源库九行直算逐行相等。"))
        else:
            checks.append(_result_check("逐行独立核对", "fail", "模型结果与源库九行直算不一致。"))

    elif kind == "clean_publish":
        asset = str(expected.get("asset") or "")
        allowed = {str(value) for value in expected.get("allowed_values", [])}
        semantics = [item for item in provenance
                     if item.get("event") == "semantics_defined"
                     and str(item.get("detail", {}).get("asset") or item.get("asset")) == asset]
        if not semantics:
            checks.append(_result_check("已确认发布口径", "pending", "尚未记录该资产的已确认口径。"))
        else:
            actual = set(str(value) for value in semantics[0].get("detail", {}).get("allowed_values", []))
            checks.append(_result_check("已确认发布口径", "pass" if actual == allowed else "fail",
                                        "允许值：" + " / ".join(sorted(actual)) if actual
                                        else "记录里缺少结构化允许值。"))
        bronze_rows = sum(int(value) for value in lake.get("bronze", {}).values())
        checks.append(_result_check("bronze 实际接入", "pass" if bronze_rows else "pending",
                                    f"bronze 共 {bronze_rows} 行。" if bronze_rows else "尚未落下 bronze。"))
        checks.append(_result_check("silver 保留原值", "pass" if silver.get("samples") else "pending",
                                    "已找到 raw 与清洗后值不同的样本。" if silver.get("samples")
                                    else "尚未找到可核对的 silver raw 样本。"))
        gold_tables = lake.get("gold", {})
        if not gold_tables:
            checks.append(_result_check("gold 发布结果", "pending", "尚未发布 gold。"))
        else:
            raw_columns = [f"{table}.{column}" for table in gold_tables
                           for column in lake_columns.get("gold", {}).get(table, [])
                           if column.endswith("_raw")]
            checks.append(_result_check("gold 发布结果", "fail" if raw_columns else "pass",
                                        "gold 含原始列：" + ", ".join(raw_columns)
                                        if raw_columns else "gold 已发布，未发现 _raw 列。"))

    elif kind == "recovery":
        failed = [event for event in events if event.get("kind") == "SOURCE_CONNECT_FAILED"]
        notices = [event for event in events if event.get("kind") == "SOURCE_CONNECT_NOTICE_SENT"]
        checks.append(_result_check("坏账号连接失败被记录", "pass" if failed else "pending",
                                    "已记录失败连接。" if failed else "尚未触发坏账号连接失败分支。"))
        if not notices:
            checks.append(_result_check("失败通知到 DBA", "pending", "尚未记录失败通知。"))
        else:
            recipients = " ".join(str(event.get("payload") or "") for event in notices).lower()
            checks.append(_result_check("失败通知到 DBA", "pass" if "dba@acme.com" in recipients else "fail",
                                        "通知收件人包含 DBA。" if "dba@acme.com" in recipients
                                        else "失败通知没有发给 DBA。"))
        transitions = [event.get("kind") for event in events
                       if event.get("kind") in ("AMEND_PENDING", "NEW_TICKET_AFTER_DECISION")]
        checks.append(_result_check("新账号后的恢复分支", "pass" if transitions else "pending",
                                    " → ".join(transitions) if transitions else "等待新账号后的真实恢复分支。"))
        bronze_rows = sum(int(value) for value in lake.get("bronze", {}).values())
        checks.append(_result_check("bronze 实际接入", "pass" if bronze_rows else "pending",
                                    f"bronze 共 {bronze_rows} 行。" if bronze_rows else "尚未落下 bronze。"))
    elif kind == "contact_request":
        source_id = str(expected.get("source_id") or "").lower()
        email = str(expected.get("email") or "").lower()
        source_contacts = [item for item in contacts
                           if str(item.get("source_id") or "").lower() == source_id]
        correct_contact = any(str(item.get("email") or "").lower() == email
                              for item in source_contacts)
        if correct_contact:
            checks.append(_result_check("联系人目录落库", "pass",
                                        f"{email} 已登记为 {source_id} 的联系人。"))
        elif source_contacts:
            checks.append(_result_check("联系人目录落库", "fail",
                                        f"{source_id} 已登记联系人，但不是 {email}。"))
        else:
            checks.append(_result_check("联系人目录落库", "pending",
                                        "等待模型调用联系人登记工具。"))
        delivered = [item for item in messages
                     if str(item.get("box") or "").lower() == email
                     and CLAW in str(item.get("from") or "").lower()]
        other_outbound = [item for item in messages
                          if CLAW in str(item.get("from") or "").lower()]
        if delivered:
            checks.append(_result_check("给数据库管理员的真实邮件", "pass",
                                        "数据库管理员的真实 GreenMail 收件箱已收到模型邮件。"))
        elif other_outbound:
            checks.append(_result_check("给数据库管理员的真实邮件", "fail",
                                        "模型已发出邮件，但数据库管理员的收件箱没有收到。"))
        else:
            checks.append(_result_check("给数据库管理员的真实邮件", "pending",
                                        "等待模型向数据库管理员发邮件。"))

    elif kind == "credential_received":
        source_id = str(expected.get("source_id") or "").lower()
        email = str(expected.get("email") or "").lower()
        credential_mail = [item for item in messages
                           if str(item.get("box") or "").lower() == CLAW
                           and email in str(item.get("from") or "").lower()
                           and "[数据库连接串已隐藏]" in str(item.get("body") or "")]
        checks.append(_result_check("数据库管理员的凭证安全到达", "pass" if credential_mail else "pending",
                                    "凭证邮件已进模型收件箱；网页中的口令始终隐藏。"
                                    if credential_mail else "等待数据库管理员的运行时凭证邮件。"))
        connect_events = [event for event in events
                          if _event_payload(event).get("tool") == "connect_source"]
        if any(event.get("kind") in ("BLOCKED_PENDING_APPROVAL", "TOOL_ok")
               for event in connect_events):
            checks.append(_result_check("按凭证发起受控连接", "pass",
                                        f"模型以 {source_id} 发起 connect_source，已进入审批/受控执行。"))
        elif any(event.get("kind") == "BLOCKED_NEED_DSN" for event in connect_events):
            checks.append(_result_check("按凭证发起受控连接", "fail",
                                        "模型已尝试接入，却没有使用数据库管理员邮件中的连接信息。"))
        else:
            checks.append(_result_check("按凭证发起受控连接", "pending",
                                        "等待模型读取邮件后决定是否发起受控接入。"))
        notices = [(_event_payload(event), event) for event in events
                   if event.get("kind") == "SOURCE_CONNECT_NOTICE_SENT"]
        successful = [payload for payload, _ in notices
                      if str(payload.get("source_id") or "").lower() == source_id
                      and not payload.get("failed")]
        failed = [payload for payload, _ in notices
                  if str(payload.get("source_id") or "").lower() == source_id
                  and payload.get("failed")]
        def outcome_mail_delivered(payload: dict) -> bool:
            targets = payload.get("to") or []
            if isinstance(targets, str):
                targets = [targets]
            subject = str(payload.get("subject") or "")
            return any(
                str(message.get("box") or "").lower() in {str(x).lower() for x in targets}
                and (str(message.get("subject") or "") == subject
                     or str(message.get("subject") or "").startswith(subject + " [#"))
                and CLAW in str(message.get("from") or "").lower()
                for message in messages)
        delivered_success = [payload for payload in successful if outcome_mail_delivered(payload)]
        if delivered_success:
            checks.append(_result_check("Northwind 实际连接成功", "pass",
                                        "连接成功，且数据库管理员/数据负责人的收件箱已收到结果。"))
        elif successful:
            checks.append(_result_check("Northwind 实际连接成功", "pending",
                                        "已记录连接成功，但尚未观察到对应的结果邮件。"))
        elif failed:
            checks.append(_result_check("Northwind 实际连接成功", "fail",
                                        "连接已执行但失败；等待新的连接信息，不应重复接入。"))
        else:
            checks.append(_result_check("Northwind 实际连接成功", "pending",
                                        "等待负责人批准后由真实 Connector 完成连接。"))

    else:
        checks.append(_result_check("Case 判据", "fail", f"未知 expected_outcome.kind：{kind}"))

    states = {check["state"] for check in checks}
    state = "fail" if "fail" in states else "pass" if states == {"pass"} else "pending"
    summary = ("Case 正确：所有预期结果均有独立证据。" if state == "pass" else
               "Case 不正确：至少一项实际结果与预期冲突。" if state == "fail" else
               "Case 尚未判完：等待剩余真实动作或数据证据。")
    return {"state": state, "expected": str(expected.get("answer") or ""),
            "checks": checks, "summary": summary}


def status() -> dict:
    current = RUNS.current()
    snapshot = active_snapshot()
    # The demo run id controls isolated reset/session hand-off.  Governance
    # records use Hermes task/action ids (one session may contain several), so
    # they cannot be equated.  The demo clears every evidence plane after the
    # reset acknowledgement; its start time is the read-only current-window
    # boundary for those nested task records.
    started_at = float((current or {}).get("started_at") or time.time())

    def approval_data():
        rows = db_rows(
            "SELECT a.id, a.tool_name, a.args_json, a.approver, a.created_at, a.expires_at, "
            "a.used_at, max(d.decided_at) "
            "FROM approvals a LEFT JOIN decisions d ON d.approval_id=a.id "
            "WHERE a.created_at >= to_timestamp(%s) "
            "GROUP BY a.id, a.tool_name, a.args_json, a.approver, a.created_at, a.expires_at, a.used_at "
            "ORDER BY a.created_at DESC LIMIT 40", (started_at,))
        return [{"id": str(r[0]), "tool": r[1], "summary": approval_summary(r[1], r[2]),
                 "approver": r[3], "created": str(r[4]), "expires": str(r[5]),
                 # Callback is the sole writer of decisions; older callback
                 # paths may not update approvals.used_at.  A decision row is
                 # still terminal and must not render as pending.
                 "used": str(r[6] or r[7]) if (r[6] or r[7]) else None}
                for r in rows]

    def events_data():
        rows = db_rows("SELECT kind, payload, ts FROM events WHERE ts >= %s "
                       "ORDER BY seq DESC LIMIT 60", (started_at,))
        return [{"kind": r[0], "payload": redact(str(r[1] or ""))[:700],
                 "time": str(r[2])} for r in rows]

    def contacts_data():
        rows = db_rows("SELECT source_id, email, display_name, relationship, recorded_at "
                       "FROM source_contacts WHERE recorded_at >= %s "
                       "ORDER BY recorded_at DESC LIMIT 40", (started_at,))
        return [{"source_id": str(r[0]), "email": str(r[1]), "display_name": str(r[2]),
                 "relationship": str(r[3]), "recorded_at": str(r[4])} for r in rows]

    def query_data():
        rows = db_rows("SELECT source_id, purpose, sql_text, rows_out, duration_ms, status, ts "
                       "FROM query_ledger ORDER BY id DESC LIMIT 50")
        return [{"source": r[0], "purpose": r[1] or "", "sql": redact(r[2])[:3000],
                 "rows": int(r[3]), "duration_ms": float(r[4]), "status": r[5],
                 "time": str(r[6])} for r in rows]

    def provenance_data():
        rows = db_rows("SELECT asset, event, actor, detail, ts FROM asset_provenance "
                       "ORDER BY id DESC LIMIT 60")
        out = []
        for asset, event, actor, detail, ts in rows:
            try:
                detail = json.loads(detail or "{}")
            except (TypeError, json.JSONDecodeError):
                detail = {"detail": redact(str(detail))}
            out.append({"asset": asset, "event": event, "actor": actor,
                        "detail": detail, "time": str(ts)})
        return out

    def source_data():
        names = tuple(snapshot.get("source_tables", ())) if snapshot else ()
        return {name: int(source_rows(f"SELECT count(*) FROM {_quoted(name)}")[0][0])
                for name in names}

    def silver_samples(lake):
        names = [name for name in lake.get("silver", {}) if "order_status" in name]
        if not names:
            return {"table": None, "samples": []}
        table = names[0]
        rows = trino(f"SELECT order_id, order_status_raw, order_status "
                     f"FROM iceberg.silver.{_quoted(table)} "
                     "WHERE order_status_raw IS DISTINCT FROM order_status LIMIT 20")
        return {"table": table, "samples": [row.split(",", 2) for row in rows]}

    def gold_data(lake):
        rows = db_rows("SELECT asset, key, value, confirmed_by FROM asset_semantics "
                       "ORDER BY confirmed_at DESC LIMIT 30")
        return {"tables": lake.get("gold", {}),
                "semantics": [{"asset": r[0], "key": r[1], "value": r[2], "by": r[3]}
                              for r in rows]}

    def sales_direct():
        rows = source_rows(
            "SELECT e.last_name || ' (' || e.employee_id || ')', "
            "round(sum(od.unit_price * od.quantity * (1 - od.discount))::numeric, 2), "
            "count(DISTINCT o.order_id), count(*) "
            "FROM order_details od JOIN orders o ON od.order_id=o.order_id "
            "JOIN employees e ON o.employee_id=e.employee_id "
            "GROUP BY e.last_name, e.employee_id ORDER BY 2 DESC")
        return [[str(value) for value in row] for row in rows]

    def answer_comparison(direct):
        rows = db_rows("SELECT detail FROM asset_provenance WHERE event='answered' "
                       "ORDER BY id DESC LIMIT 30")
        source = direct if isinstance(direct, list) else []
        source_norm = sorted(_answer_row(row) for row in source)
        for (detail,) in rows:
            try:
                detail = json.loads(detail or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            question = str(detail.get("question") or "")
            result = detail.get("result")
            if not result or not ("销售" in question or "seller" in question.lower()
                                  or "sales" in question.lower()):
                continue
            model_norm = sorted(_answer_row(row) for row in result)
            return {"matched": bool(source_norm and model_norm == source_norm),
                    "model_rows": result, "source_rows": source,
                    "question": question}
        return {"matched": False, "model_rows": [], "source_rows": source,
                "question": ""}

    approvals = safe(approval_data, lambda error: {"error": error})
    events = safe(events_data, lambda error: {"error": error})
    contacts = safe(contacts_data, lambda error: [])
    messages = safe(mailboxes, lambda error: [])
    provenance = safe(provenance_data, lambda error: {"error": error})
    lake = safe(lake_data, lambda error: {"error": error})
    lake_columns = safe(lambda: lake_columns_data(lake), lambda error: {})
    direct = safe(sales_direct, lambda error: [])
    silver = safe(lambda: silver_samples(lake), lambda error: {"error": error})
    gold = safe(lambda: gold_data(lake), lambda error: {"error": error})
    comparison = safe(lambda: answer_comparison(direct), lambda error: {"matched": False,
                                                                          "model_rows": [],
                                                                          "source_rows": direct,
                                                                          "question": ""})
    case_map, _ = cases()
    case_result = safe(lambda: evaluate_case(case_map.get(str((current or {}).get("case_id") or "")),
                                              snapshot,
                                              events=events if isinstance(events, list) else [],
                                              lake=lake if isinstance(lake, dict) else {},
                                              lake_columns=lake_columns if isinstance(lake_columns, dict) else {},
                                              silver=silver if isinstance(silver, dict) else {},
                                              provenance=provenance if isinstance(provenance, list) else [],
                                              answer_comparison=comparison if isinstance(comparison, dict) else {},
                                              messages=messages if isinstance(messages, list) else [],
                                              contacts=contacts if isinstance(contacts, list) else []),
                       lambda error: {"state": "pending", "expected": "", "checks": [],
                                      "summary": "Case 判据暂不可读。"})
    return {
        "agent": agent_state(), "run": current, "snapshot": public_snapshot(snapshot),
        "environment": {"snapshot_ready": snapshot_dependencies_ready()},
        "approvals": approvals,
        "events": events,
        "contacts": contacts,
        "queries": safe(query_data, lambda error: {"error": error}),
        "provenance": provenance,
        "source": safe(source_data, lambda error: {"error": error}),
        "lake": lake,
        "lake_columns": lake_columns,
        "silver": silver,
        "gold": gold,
        "source_sales": direct,
        "answer_comparison": comparison,
        "case_result": case_result,
        "updated_at": int(time.time()),
    }


def send_mail(data: dict) -> dict:
    frm, subject, body = (str(data.get(key, "")).strip() for key in ("from", "subject", "body"))
    if frm not in PEOPLE or not subject or not body or len(body) > 6000:
        raise ValueError("invalid rehearsal email")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, CLAW, subject
    msg["Date"], msg["Message-ID"] = formatdate(), make_msgid(domain="acme-sim.test")
    domain = frm.rsplit("@", 1)[-1]
    msg["Authentication-Results"] = f"mx.acme-sim.test; dmarc=pass header.from={domain}"
    msg["X-Demo-Actor"] = "human"
    msg.set_content(body)
    with smtplib.SMTP(HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.send_message(msg)
    RUNS.append("observations", {"kind": "human_mail", "at": time.time(),
                                  "from": frm, "subject": subject})
    return {"accepted": True, "message_id": msg["Message-ID"]}


def clear_mail() -> None:
    for box in PEOPLE + (CLAW,):
        im = imaplib.IMAP4(HOST, IMAP_PORT, timeout=10)
        try:
            im.login(box, box)
            im.select("INBOX")
            _, ids = im.search(None, "ALL")
            for uid in ids[0].split() if ids and ids[0] else []:
                im.store(uid, "+FLAGS", "\\Deleted")
            im.expunge()
        finally:
            try:
                im.logout()
            except Exception:  # noqa: BLE001
                pass


def clear_governance() -> None:
    names = admin_rows(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'")
    if names:
        tables = ", ".join(_quoted(str(row[0])) for row in names)
        admin_exec(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")


def ensure_demo_schema() -> None:
    """Apply the small, idempotent demo schema migration before a new run.

    Docker's initdb hook only runs for a brand-new named volume. A developer
    keeping a demo volume therefore needs this migration before the source
    connection idempotency gate can read the non-secret connection identity.
    The migration executes with the demo admin account, never in the browser
    or the Agent role.
    """
    migration = ROOT / "infra" / "migrations" / "20260911_source_secret_identity.sql"
    admin_exec(migration.read_text(encoding="utf-8"))


def clear_lake() -> None:
    for schema in ("gold", "silver", "bronze"):
        tables = trino("SELECT table_name FROM iceberg.information_schema.tables "
                       f"WHERE table_schema='{schema}'")
        for row in tables:
            trino(f"DROP TABLE IF EXISTS iceberg.{schema}.{_quoted(row.split(',')[-1])}")


def request_agent_reset(run_id: str) -> None:
    request_dir = CONTROL_DIR / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    target = request_dir / f"{run_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_id": run_id, "requested_at": time.time()}),
                   encoding="utf-8")
    tmp.replace(target)


def reset_acknowledged(run_id: str) -> bool:
    """The gateway must acknowledge this exact run before mail is delivered."""
    return (CONTROL_DIR / "acknowledged" / f"{run_id}.json").is_file()


def case_packet(case: dict, snapshot: dict) -> dict:
    history = "\n\n".join(
        f"--- 已发生邮件 · {item['from']} · {item['subject']} ---\n{item['body']}"
        for item in snapshot.get("history", case.get("history", [])))
    opening = snapshot.get("opening") or case.get("opening")
    if not isinstance(opening, dict):
        raise ValueError("Snapshot is missing opening mail")
    opening_body = str(opening["body"])
    # Credentials belong to the isolated runtime, never to cases.json or the
    # browser metadata.  GreenMail receives the complete email for Hermes;
    # fetch()/public_mail() redact the password before any page response.
    if snapshot.get("private_opening") == "demo_source_dsn":
        dsn = os.environ.get("DEMO_SOURCE_DSN", "").strip()
        if not dsn.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("DEMO_SOURCE_DSN is unavailable for credential Case")
        opening_body += "\n\n连接串（仅按受控接入流程使用，不要复述）：\n" + dsn
    table_scope = str(snapshot.get("table_scope") or
                      (", ".join(snapshot["source_tables"])
                       if snapshot.get("source_tables")
                       else "本轮未指定任何业务表。"))
    body = (
        f"【演练 Case：{case['title']}】\n"
        f"【数据库 Snapshot：{snapshot['title']} / {snapshot['database']}】\n"
        f"本轮表范围：{table_scope}\n"
        f"说明：{snapshot['note']}\n\n"
        f"【本 Snapshot 的受控范围】\n"
        f"仅可执行：{', '.join(snapshot.get('tool_scope', [])) or '无'}。"
        f"达到「{snapshot.get('terminal_condition') or '本轮预期'}」后停止，"
        "不要延伸到后续流程。\n\n"
        f"【此前人员邮件记录】\n{history}\n\n"
        f"【当前邮件 · {opening['from']}】\n{opening_body}"
    )
    return {"from": opening["from"], "subject": opening["subject"], "body": body}


def deliver_after_gateway(run_id: str, case: dict, snapshot: dict) -> None:
    deadline = time.time() + 180
    acknowledged = False
    while time.time() < deadline:
        if reset_acknowledged(run_id):
            if not acknowledged:
                acknowledged = True
                # The previous gateway can still have emitted its shutdown
                # notice while the reset request was in flight.  Only after
                # the agent acknowledges termination is it safe to clean the
                # mail, governance, and lake planes for this exact run.
                try:
                    ensure_demo_schema()
                    clear_lake()
                    clear_governance()
                    clear_mail()
                except Exception as exc:                    # noqa: BLE001
                    RUNS.update_current(state="snapshot_failed",
                                        error=type(exc).__name__)
                    return
                RUNS.update_current(state="reset_acknowledged",
                                    reset_acknowledged_at=time.time())
        state = agent_state()
        if (acknowledged and state.get("state") == "ready"
                and state.get("run_id") == run_id):
            try:
                send_mail(case_packet(case, snapshot))
                RUNS.update_current(state="case_delivered", delivered_at=time.time())
            except Exception as exc:  # noqa: BLE001
                RUNS.update_current(state="delivery_failed", error=type(exc).__name__)
            return
        if state.get("state") == "blocked":
            RUNS.update_current(state="gateway_blocked", error=state.get("detail", ""))
            return
        time.sleep(1)
    RUNS.update_current(state="reset_timeout" if not acknowledged else "gateway_timeout")


def start_run(case_id: str, snapshot_id: str) -> dict:
    case_map, snapshot_map = cases()
    case, snapshot = case_map.get(case_id), snapshot_map.get(snapshot_id)
    if not case or not snapshot or snapshot_id not in case.get("snapshots", []):
        raise ValueError("invalid case / snapshot combination")
    if not snapshot_dependencies_ready():
        raise DemoNotReady
    run = RUNS.begin(case, snapshot)
    try:
        request_agent_reset(run["id"])
    except Exception as exc:  # noqa: BLE001
        RUNS.update_current(state="snapshot_failed", error=type(exc).__name__)
        raise RuntimeError(type(exc).__name__) from exc
    threading.Thread(target=deliver_after_gateway, args=(run["id"], case, snapshot),
                     daemon=True).start()
    return run


def approve(link_id: str) -> dict:
    links = approval_index()
    public = links.get(link_id)
    if not public:
        raise ValueError("approval link is not in a real mailbox")
    parsed = urllib.parse.urlparse(public)
    if parsed.path not in ("/approve", "/deny", "/choose") or not parsed.query:
        raise ValueError("invalid approval link")
    internal = APPROVAL_INTERNAL_URL + parsed.path + "?" + parsed.query
    try:
        with urllib.request.urlopen(internal, timeout=15) as response:
            body = response.read().decode("utf-8", "replace")
            result = {"status": response.status, "message": re.sub("<[^>]+>", " ", body)[:700]}
    except urllib.error.HTTPError as exc:
        result = {"status": exc.code, "message": re.sub("<[^>]+>", " ", exc.read().decode(
            "utf-8", "replace"))[:700]}
    RUNS.append("approval_attempts", {"at": time.time(), "link": link_id, **result})
    return result


def verification() -> list[dict]:
    """Read current evidence only.  It never sends mail, changes a decision, or runs SQL."""
    current = RUNS.current()
    state = agent_state()
    checks = []
    case_map, _ = cases()
    active_case = case_map.get(str((current or {}).get("case_id") or ""), {})
    expected = set(active_case.get("expects", []))
    run_id = str((current or {}).get("id") or "")
    started_at = float((current or {}).get("started_at") or time.time())
    model_ready = (state.get("state") == "ready"
                   and state.get("model_probe") == "pass"
                   and bool(run_id) and state.get("run_id") == run_id)
    checks.append({"name": "模型与 tool calling", "state":
                   "pass" if model_ready else "pending",
                   "evidence": (f"state={state.get('state')} probe={state.get('model_probe')} "
                                f"agent_run={state.get('run_id') or '-'} "
                                f"page_run={run_id or '-'}")})
    messages = safe(mailboxes, lambda error: [])
    checks.append({"name": "GreenMail 真实收发", "state":
                   "pass" if messages else "pending",
                   "evidence": f"{len(messages)} 封当前邮箱邮件"})
    permission_error = ""
    try:
        agent_can_write = db_rows(
            "SELECT has_table_privilege('agent_role', 'decisions', 'INSERT')")[0][0]
    except Exception as exc:                                # noqa: BLE001
        agent_can_write = None
        permission_error = f"agent={type(exc).__name__}"
    try:
        source_can_write = source_rows(
            "SELECT has_table_privilege('ops_reader', 'orders', 'INSERT')")[0][0]
    except Exception as exc:                                # noqa: BLE001
        source_can_write = None
        permission_error = (permission_error + " " if permission_error else "") \
            + f"source={type(exc).__name__}"
    if agent_can_write is None or source_can_write is None:
        checks.append({"name": "Agent 决策权限与源库只读", "state": "pending",
                       "evidence": "权限证据不可读：" + permission_error})
    else:
        checks.append({"name": "Agent 无 decisions 写权限", "state":
                       "pass" if not agent_can_write else "fail",
                       "evidence": f"agent_role INSERT decisions = {agent_can_write}"})
        checks.append({"name": "源库无写权限", "state":
                       "pass" if not source_can_write else "fail",
                       "evidence": f"ops_reader INSERT orders = {source_can_write}"})
    event_rows = safe(lambda: db_rows(
        "SELECT kind, payload FROM events WHERE ts >= %s ORDER BY seq DESC LIMIT 160",
        (started_at,)),
        lambda error: [])
    event_data = []
    for kind, payload in event_rows:
        try:
            parsed = json.loads(payload or "{}")
        except (TypeError, json.JSONDecodeError):
            parsed = {"raw": redact(str(payload or ""))}
        event_data.append((kind, parsed))
    bound_notice = None
    for kind, payload in event_data:
        if kind != "SOURCE_CONNECT_NOTICE_SENT" or not payload.get("failed"):
            continue
        targets = payload.get("to") or []
        if isinstance(targets, str):
            targets = [targets]
        for message in messages:
            if (message.get("box") in targets
                    and (message.get("subject") == payload.get("subject")
                         or str(message.get("subject") or "").startswith(
                             str(payload.get("subject") or "") + " [#"))
                    and ("失败" in message.get("body", "")
                         or "连不上" in message.get("body", ""))):
                bound_notice = payload
                break
        if bound_notice:
            break
    checks.append({"name": "错误账号失败通知发给 DBA", "state":
                   "pass" if bound_notice else "pending",
                   "evidence": (f"事件={bound_notice.get('source_id')} → "
                                f"邮件={bound_notice.get('to')} · "
                                f"{bound_notice.get('subject')}"
                                if bound_notice else
                                "等待 SOURCE_CONNECT_FAILED 与对应 DBA 邮件绑定")})
    transitions = [kind for kind, _ in event_data
                   if kind in ("AMEND_PENDING", "NEW_TICKET_AFTER_DECISION")]
    checks.append({"name": "待决票 amend / 已决票新建", "state":
                   "pass" if transitions else "pending",
                   "evidence": ", ".join(transitions) or "等待真实参数变更分支"})
    attempts = (current or {}).get("approval_attempts", [])
    checks.append({"name": "审批链接重放被拒", "state":
                   "pass" if any(item.get("status") == 409 for item in attempts) else "pending",
                   "evidence": "在同一真实审批按钮上再次点击后核对 409"})
    lake = safe(lake_data, lambda error: {})
    lake_columns = safe(lambda: lake_columns_data(lake), lambda error: {})
    checks.append({"name": "bronze 实际产物", "state":
                   "pass" if any(int(rows) > 0 for rows in lake.get("bronze", {}).values())
                   else "pending",
                   "evidence": json.dumps(lake.get("bronze", {}), ensure_ascii=False)})
    silver = safe(lambda: status()["silver"], lambda error: {})
    checks.append({"name": "silver raw 与洗后值", "state":
                   "pass" if silver.get("samples") else "pending",
                   "evidence": silver.get("table") or "未产生 silver 样本"})
    gold = safe(lambda: status()["gold"], lambda error: {})
    gold_tables = lake.get("gold", {})
    gold_schema = lake_columns.get("gold", {})
    gold_ok = bool(gold_tables) and all(
        gold_schema.get(table) and not any(col.endswith("_raw")
                                           for col in gold_schema[table])
        for table in gold_tables)
    checks.append({"name": "gold 发布且不含 raw", "state":
                   "pass" if gold_ok else "pending",
                   "evidence": json.dumps({"tables": gold.get("tables", {}),
                                            "columns": gold_schema},
                                           ensure_ascii=False)})
    evidence = safe(status, lambda error: {})
    comparison = evidence.get("answer_comparison", {})
    direct = evidence.get("source_sales", [])
    source_sales_rows = comparison.get("source_rows", direct)
    checks.append({"name": "跨表回答与九行独立直算", "state":
                   "pass" if comparison.get("matched") and len(source_sales_rows) == 9 else "pending",
                   "evidence": (f"model_rows={len(comparison.get('model_rows', []))}，"
                                f"source_rows={len(source_sales_rows)}，"
                                f"逐行相等={comparison.get('matched', False)}")})
    # A Case declares which evidence it is intended to exercise.  Keep the
    # complete catalogue visible, but do not turn an intentionally untested
    # branch into a misleading pending result.
    if expected:
        for item in checks:
            if item["name"] not in expected:
                item["state"] = "n/a"
                item["evidence"] = "不适用于当前 Case"
    RUNS.append("verification", {"at": time.time(), "checks": checks})
    return checks


PAGE = pathlib.Path(__file__).resolve().parent / "demo_ui.html"


def page() -> str:
    """The page is a separate file so the markup is reviewable and hot-editable.

    It is read per request on purpose: ``services/`` is bind-mounted read-only
    into the demo container, so editing the HTML and refreshing the browser is
    enough.  The old inline string needed two ``str.replace`` escape fixups
    that silently broke the whole script when either one stopped matching.
    """
    return PAGE.read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, code: int, data, content_type="application/json; charset=utf-8"):
        raw = data.encode() if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 7000:
            raise ValueError("payload too large")
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, page(), "text/html; charset=utf-8")
        if self.path == "/api/meta":
            return self.reply(200, {"cases": public_cases(),
                                    "snapshots": public_snapshots()})
        if self.path == "/api/status":
            return self.reply(200, status())
        if self.path == "/api/mail":
            try:
                messages = mailboxes()
                # Reading the inbox is observational only.  Do not append the
                # complete mailbox to run history on every five-second refresh.
                return self.reply(200, public_mail(messages))
            except Exception as exc:  # noqa: BLE001
                return self.reply(503, {"error": type(exc).__name__})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            data = self.body()
            if self.path == "/api/send":
                return self.reply(202, send_mail(data))
            if self.path == "/api/run/start":
                return self.reply(202, start_run(str(data.get("case_id", "")),
                                                 str(data.get("snapshot_id", ""))))
            if self.path == "/api/approval":
                return self.reply(200, approve(str(data.get("id", ""))))
            if self.path == "/api/verify":
                return self.reply(200, verification())
            return self.reply(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.reply(400, {"error": type(exc).__name__})
        except DemoNotReady:
            return self.reply(503, {"error": "演练环境仍在准备，请等数据服务就绪后再试"})
        except Exception as exc:  # noqa: BLE001
            return self.reply(503, {"error": type(exc).__name__})


if __name__ == "__main__":
    print(f"demo UI → http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
