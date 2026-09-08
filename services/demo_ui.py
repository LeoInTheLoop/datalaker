#!/usr/bin/env python3
"""One-page observer for a real Hermes / data-steward rehearsal.

This is deliberately a thin adapter.  It never imports a data-steward tool,
never writes a decision, and never chooses a model action.  Its only writes
are human simulation mail, isolated demo snapshot reset requests, and its own
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
MAX_OBSERVATIONS = 120


def redact(text: str) -> str:
    """Credentials and one-time approval tokens never leave the adapter."""
    value = str(text or "")
    value = APPROVAL_URL_RE.sub("[审批链接已隐藏]", value)
    return re.sub(r"(postgres(?:ql)?://[^:\s/]+:)[^@\s/]+@",
                  r"\1<已隐藏>@", value)


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


def status() -> dict:
    snapshot = active_snapshot()

    def approval_data():
        rows = db_rows(
            "SELECT a.id, a.tool_name, a.approver, a.created_at, a.expires_at, "
            "a.used_at, max(d.decided_at) "
            "FROM approvals a LEFT JOIN decisions d ON d.approval_id=a.id "
            "GROUP BY a.id, a.tool_name, a.approver, a.created_at, a.expires_at, a.used_at "
            "ORDER BY a.created_at DESC LIMIT 40")
        return [{"id": str(r[0]), "tool": r[1], "approver": r[2],
                 "created": str(r[3]), "expires": str(r[4]),
                 # Callback is the sole writer of decisions; older callback
                 # paths may not update approvals.used_at.  A decision row is
                 # still terminal and must not render as pending.
                 "used": str(r[5] or r[6]) if (r[5] or r[6]) else None}
                for r in rows]

    def events_data():
        rows = db_rows("SELECT kind, payload, ts FROM events ORDER BY seq DESC LIMIT 60")
        return [{"kind": r[0], "payload": redact(str(r[1] or ""))[:700],
                 "time": str(r[2])} for r in rows]

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

    lake = safe(lake_data, lambda error: {"error": error})
    lake_columns = safe(lambda: lake_columns_data(lake), lambda error: {})
    direct = safe(sales_direct, lambda error: [])
    return {
        "agent": agent_state(), "run": RUNS.current(), "snapshot": snapshot,
        "approvals": safe(approval_data, lambda error: {"error": error}),
        "events": safe(events_data, lambda error: {"error": error}),
        "queries": safe(query_data, lambda error: {"error": error}),
        "provenance": safe(provenance_data, lambda error: {"error": error}),
        "source": safe(source_data, lambda error: {"error": error}),
        "lake": lake,
        "lake_columns": lake_columns,
        "silver": safe(lambda: silver_samples(lake), lambda error: {"error": error}),
        "gold": safe(lambda: gold_data(lake), lambda error: {"error": error}),
        "source_sales": direct,
        "answer_comparison": safe(lambda: answer_comparison(direct),
                                   lambda error: {"matched": False,
                                                  "model_rows": [],
                                                  "source_rows": direct,
                                                  "question": ""}),
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
        for item in case.get("history", []))
    opening = case["opening"]
    body = (
        f"【演练 Case：{case['title']}】\n"
        f"【数据库 Snapshot：{snapshot['title']} / {snapshot['database']}】\n"
        f"可用的本轮源表：{', '.join(snapshot['source_tables'])}\n"
        f"说明：{snapshot['note']}\n\n"
        f"【此前人员邮件记录】\n{history}\n\n"
        f"【当前邮件 · {opening['from']}】\n{opening['body']}"
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
    run = RUNS.begin(case, snapshot)
    try:
        clear_lake()
        clear_governance()
        clear_mail()
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
        "SELECT kind, payload FROM events ORDER BY seq DESC LIMIT 160"),
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
        for message in messages:
            if (message.get("box") == payload.get("to")
                    and message.get("subject") == payload.get("subject")
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


HTML = """<!doctype html><html lang=zh-CN><meta charset=utf-8>
<title>Data Steward Claw · 真模型演练</title>
<style>
body{font:14px/1.5 system-ui,-apple-system,sans-serif;background:#f6f7f9;color:#17202a;margin:0}main{max-width:1500px;margin:auto;padding:22px}h1{margin:0}h2{font-size:17px;margin:0 0 10px}.hint{color:#59636e}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:15px;margin-top:16px}.wide{grid-column:1/-1}.card{background:#fff;border:1px solid #e2e7ec;border-radius:10px;padding:15px}.ready,.pass{color:#087443}.blocked,.fail{color:#b42318}.pending{color:#a15c00}.pill{padding:3px 7px;background:#edf2f7;border-radius:999px;font-size:12px}button{margin:3px;padding:7px 10px;border:1px solid #aab6c2;border-radius:6px;background:#fff;cursor:pointer}textarea,input,select{box-sizing:border-box;width:100%;margin:4px 0;padding:7px;font:inherit}pre{white-space:pre-wrap;overflow:auto;background:#f8fafc;padding:9px;border-radius:6px;max-height:310px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #e5e7eb;padding:5px;text-align:left;vertical-align:top}.mail{border-top:1px solid #e5e7eb;padding:10px 0}.mail:first-child{border-top:0}.approval{margin:2px;color:#135bc1}#error{color:#b42318}
</style><main>
<h1>Data Steward Claw · 真模型演练</h1>
<p class=hint>页面只投递演练人员邮件、转发你点击的真实审批链接、读取实际证据与执行只读真测。Hermes 决定模型回复和治理工具调用。</p>
<p id=agent></p><p id=error></p>
<div class=grid>
<section class=card><h2>固定 Case 与数据库 Snapshot</h2><select id=case></select><select id=snapshot></select><button id=start>开始本轮演练</button><button id=verify>运行只读真测</button><p id=run class=hint>尚未开始。</p><div id=checks></div></section>
<section class=card><h2>演练人员邮件</h2><form id=mailForm><select id=from><option>boss@acme.com</option><option>wang@acme.com</option><option>dba@acme.com</option></select><input id=subject placeholder=主题 required><textarea id=body rows=5 placeholder=邮件正文 required></textarea><button>发送真实邮件</button></form><p class=hint>例如 DBA 先给错账号、再给正确账号；页面不会替模型调用工具。</p></section>
<section class=card><h2>审批与模型事件</h2><div id=approvals></div><pre id=events></pre></section>
<section class=card><h2>模型实际 SQL / 工具证据</h2><div id=queries></div><pre id=provenance></pre></section>
<section class=card><h2>数据层证据</h2><div id=data></div></section>
<section class=card><h2>独立源库核对：最佳销售</h2><div id=sales></div><p class=hint>此表由页面独立只读源库直算；模型实际 answer_with_link 证据另列显示。</p></section>
<section class="card wide"><h2>GreenMail 收件箱</h2><div id=mail></div></section>
</div></main>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let meta={};
async function api(path,method='GET',body){let r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d=await r.json();if(!r.ok)throw new Error(d.error||r.status);return d}
function options(){let selected=$('#case').value;$('#case').innerHTML=meta.cases.map(x=>'<option value="'+esc(x.id)+'">'+esc(x.title)+'</option>').join('');if(selected)$('#case').value=selected;let c=meta.cases.find(x=>x.id===$('#case').value)||meta.cases[0];$('#snapshot').innerHTML=c.snapshots.map(id=>{let x=meta.snapshots.find(s=>s.id===id);return '<option value="'+esc(id)+'">'+esc(x.title)+' · '+esc(x.database)+'</option>'}).join('')}
async function boot(){meta=await api('/api/meta');options();$('#case').onchange=options;$('#start').onclick=async()=>{try{let r=await api('/api/run/start','POST',{case_id:$('#case').value,snapshot_id:$('#snapshot').value});error.textContent='已建立 '+r.id+'；正在还原快照并等待 GreenMail 网关就绪。';refresh()}catch(e){error.textContent=e.message}};$('#verify').onclick=async()=>{try{renderChecks(await api('/api/verify','POST'));refresh()}catch(e){error.textContent=e.message}};$('#mailForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/send','POST',{from:$('#from').value,subject:$('#subject').value,body:$('#body').value});error.textContent='已送入 GreenMail，等待真实网关处理。';refresh()}catch(e){error.textContent=e.message}};refresh()}
async function clickApproval(id){try{let r=await api('/api/approval','POST',{id});error.textContent='审批 callback：HTTP '+r.status+' · '+r.message.replace(/\\s+/g,' ').trim();refresh()}catch(e){error.textContent=e.message}}
function renderChecks(a){checks.innerHTML=(a||[]).map(x=>'<p class="'+x.state+'"><b>'+esc(x.name)+'</b>：'+esc(x.state)+' · '+esc(x.evidence)+'</p>').join('')}
function table(rows,heads){return '<table><tr>'+heads.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join('')+'</table>'}
async function refresh(){try{let s=await api('/api/status');agent.innerHTML='网关：<b class="'+(s.agent.state==='ready'?'ready':'blocked')+'">'+esc(s.agent.state)+'</b> '+esc(s.agent.detail||'');run.textContent=s.run?('本轮 '+s.run.id+' · '+s.run.case_title+' · '+s.run.snapshot_title+' · '+s.run.state):'尚未开始。';approvals.innerHTML=Array.isArray(s.approvals)?table(s.approvals.map(x=>[x.tool,x.approver,x.used?'已决':'待决']),['动作','审批人','状态']):esc(s.approvals.error);if(Array.isArray(s.approvals))approvals.querySelectorAll('tr').forEach((tr,i)=>{let x=s.approvals[i-1];if(x&&!x.used){let b=document.createElement('button');b.textContent='到本页邮件中处理';b.className='approval';b.onclick=()=>$('#mail').scrollIntoView({behavior:'smooth'});tr.lastChild.append(b)}});events.textContent=Array.isArray(s.events)?s.events.map(x=>x.kind+' · '+x.payload).join('\n'):s.events.error||'';queries.innerHTML=Array.isArray(s.queries)?table(s.queries.map(x=>[x.source,x.status,x.rows,x.duration_ms+'ms',x.sql]),['源','状态','行数','耗时','模型实际 SQL']):esc(s.queries.error);provenance.textContent=Array.isArray(s.provenance)?JSON.stringify(s.provenance,null,2):s.provenance.error||'';data.innerHTML='<pre>'+esc(JSON.stringify({source:s.source,lake:s.lake,lake_columns:s.lake_columns,silver:s.silver,gold:s.gold,answer_comparison:s.answer_comparison},null,2))+'</pre>';sales.innerHTML=Array.isArray(s.source_sales)?table(s.source_sales,['销售','销售额','订单','明细行']):'未能直算：'+esc(s.source_sales.error);let m=await api('/api/mail');mail.innerHTML=m.map(x=>'<article class=mail><span class=pill>'+esc(x.origin)+'</span> <b>'+esc(x.box)+'</b> ← '+esc(x.from)+'<br><b>'+esc(x.subject)+'</b><pre>'+esc(x.body)+'</pre><div>'+x.links.map(l=>'<button data-link="'+esc(l.id)+'" onclick="clickApproval(\''+esc(l.id)+'\')">'+esc(l.action==='approve'?'批准':l.action==='deny'?'拒绝':'选择')+'（真实审批）</button>').join('')+'</div></article>').join('')||'尚无邮件'}catch(e){error.textContent='读取失败：'+e.message}}
boot();setInterval(refresh,5000);
</script></html>"""

# The Python template would otherwise turn the JavaScript "\\n" escape into a
# literal newline inside the quoted string, which makes the browser reject the
# whole script before the Case selectors can be populated.
HTML = HTML.replace("join('" + chr(10) + "')", "join('\\n')")
HTML = HTML.replace("clickApproval(''+esc(l.id)+'')",
                    "clickApproval(\\'" + "'+esc(l.id)+'" + "\\')")


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
            return self.reply(200, HTML, "text/html; charset=utf-8")
        if self.path == "/api/meta":
            case_map, snapshot_map = cases()
            return self.reply(200, {"cases": list(case_map.values()),
                                    "snapshots": list(snapshot_map.values())})
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
        except Exception as exc:  # noqa: BLE001
            return self.reply(503, {"error": type(exc).__name__})


if __name__ == "__main__":
    print(f"demo UI → http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
