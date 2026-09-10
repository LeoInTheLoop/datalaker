"""接入这条路上的六个坑（2026-09-06 northwind 实测抓到）。

这一组盯的不是「接入能不能成功」——那早就有测试。盯的是**接入失败之后
会发生什么**：谁收到消息、会不会拿坏账号重试、人发来新账号会不会被吞掉、
以及做完的事会不会每一轮都再申请一次审批。

六条都来自一次真模型演练，不是设想出来的：

1. 连不上时通知发给了 owner，而账号是 dba 给的 —— 能修的人不知道
2. 拿已经证明连不通的账号反复重试（且开了一张人也处理不了的审批票）
3. 人发来修正后的账号，被挂到旧票上，回填时又换回旧账号
4. 新加的门禁把「恢复」自己挡住了（`if not rows` 当成走错后端）
5. `bytea` 蒙成 varchar，错推迟到插数那一步，报错里不带列名
6. 接过的表每一轮唤醒都重新申请一次审批，人白点九次链接

    python3 tests/test_connect_retry.py
"""
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

DB = "/tmp/dl_retry_test.db"
for suf in ("", "-wal", "-shm"):
    f = pathlib.Path(DB + suf)
    if f.exists():
        f.unlink()
os.environ["DATASTEWARD_DB"] = DB
os.environ.pop("DATASTEWARD_DSN", None)
os.environ.setdefault("NOTIFY_CHANNEL", "outbox")
os.environ.setdefault("NOTIFY_OUTBOX", "/tmp/dl_retry_outbox.jsonl")
pathlib.Path(os.environ["NOTIFY_OUTBOX"]).unlink(missing_ok=True)

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


import sync                                                   # noqa: E402
import connector                                               # noqa: E402
from datasteward_gate import (_already_done, _args_changed,    # noqa: E402
                              _connect_without_dsn, _usable_dsn_exists,
                              _gate as run_gate)
from datasteward_gate.approvals import open_store, rows_of     # noqa: E402

BAD = "postgresql://crm_reader:Crmro88@127.0.0.1:5432/northwind"
GOOD = "postgresql://ops_reader:Opsread7@127.0.0.1:5432/northwind"

print("\n=== 坑 4：两个后端的分发，别拿「结果是空的」当信号 ===\n")

with open_store(readonly=False, init_schema=True) as st:
    empty = rows_of(st, "SELECT payload FROM events WHERE kind = {0}", ("没有这种",))
    chk("**空结果就是空列表，不是异常、不是换后端**", empty == [], str(empty))
    chk("查得到的照常返回",
        isinstance(rows_of(st, "SELECT count(*) FROM approvals"), list))

print("\n=== 坑 2：没有可用连接串时，拒绝且不发审批 ===\n")

with open_store(readonly=False, init_schema=True) as st:
    m = _connect_without_dsn(st, "connect_source", {"source_id": "northwind"})
    chk("**一张票都没有时，不带 dsn 直接被拒**",
        bool(m) and "NEED_DSN" in m, str(m)[:80])
    chk("拒绝理由说清了「现在发审批没有意义」",
        "没有意义" in (m or ""), str(m)[:110])
    chk("带了 dsn 就照常走审批（不拦）",
        _connect_without_dsn(st, "connect_source",
                             {"source_id": "northwind", "dsn": BAD}) is None)
    chk("别的工具不受影响",
        _connect_without_dsn(st, "ingest_table", {"source": "x"}) is None)

# 造一张「已批准」的票，模拟人批过 BAD 这份账号
with open_store(readonly=False, init_schema=True) as st:
    h = st.action_hash("connect_source", {"source_id": "northwind"})
    aid, _ = st.request("r1", h, "connect_source",
                        json.dumps({"source_id": "northwind", "dsn": BAD}),
                        "sponsor")
    st.decide(aid, "approve", "boss@acme.com")
    chk("批过之后，恢复（不带 dsn）能走通",
        _usable_dsn_exists(st, "northwind")
        and _connect_without_dsn(st, "connect_source",
                                 {"source_id": "northwind"}) is None)

    # 连不上，记一条失败
    st.append_event("r1", "SOURCE_CONNECT_FAILED", json.dumps(
        {"source_id": "northwind",
         "identity": "crm_reader@127.0.0.1:5432/northwind",
         "error": "permission denied"}))
    chk("**证明连不通之后，那份账号不再算「可用」**",
        not _usable_dsn_exists(st, "northwind"))
    m = _connect_without_dsn(st, "connect_source", {"source_id": "northwind"})
    chk("**于是空手重试被拒**（不会拿坏账号再连一次）",
        bool(m) and "NEED_DSN" in m, str(m)[:70])
    chk("失败事件里没有口令",
        all("Crmro88" not in str(r[0]) for r in rows_of(
            st, "SELECT payload FROM events")),
        "只记 user@host:port/db")

print("\n=== PostgreSQL 时间类型兼容 ===\n")

import datasteward_gate as _gate
import datasteward_gate.approvals as _approvals
_real_rows_of = _approvals.rows_of
_approvals.rows_of = lambda *_args, **_kwargs: [
    (datetime.now(timezone.utc), 830)]
try:
    _recent = _gate._recent_sync(object(), "northwind.orders")
    chk("PostgreSQL datetime 不会让重复接入门禁报 GATE_ERROR",
        bool(_recent) and "刚接过" in _recent, str(_recent))
finally:
    _approvals.rows_of = _real_rows_of

print("\n=== 错误账号不能被元数据清单误判为可用 ===\n")

_real_list_tables = connector.list_tables
_real_describe_table = connector.describe_table
connector.list_tables = lambda _source: [("orders", 830)]
connector.describe_table = lambda _source, _table: {"columns": [], "primary_key": []}
try:
    connector.validate_read_access("northwind")
    chk("无任何可读列的账号在接入时被拒", False)
except connector.ConnectorError as e:
    chk("无任何可读列的账号在接入时被拒", "没有任何可读表" in str(e), str(e))
finally:
    connector.list_tables = _real_list_tables
    connector.describe_table = _real_describe_table

print("\n=== 坑 3：人发来新账号，不能被吞掉 ===\n")

with open_store(readonly=False, init_schema=True) as st:
    h2 = st.action_hash("connect_source", {"source_id": "acme2"})
    a2, created = st.request("r2", h2, "connect_source",
                             json.dumps({"source_id": "acme2"}), "sponsor")
    chk("先有一张参数里没有账号的待批票", created)
    chk("**参数变了认得出来**",
        _args_changed(st, a2, {"source_id": "acme2", "dsn": GOOD}))
    chk("参数没变认得出没变",
        not _args_changed(st, a2, {"source_id": "acme2"}))
    chk("**待批票可以改**（还没人做过决定）",
        st.amend_pending(a2, json.dumps({"source_id": "acme2", "dsn": GOOD})))
    got = json.loads(rows_of(st, "SELECT args_json FROM approvals"
                                 " WHERE id = {0}", (a2,))[0][0])
    chk("改完之后票里是新账号（人批的时候看得见它）",
        got.get("dsn") == GOOD, str(got))

    st.decide(a2, "approve", "boss@acme.com")
    chk("**批过的票一律不动**（改它就是伪造授权）",
        not st.amend_pending(a2, json.dumps({"source_id": "acme2", "dsn": BAD})))
    still = json.loads(rows_of(st, "SELECT args_json FROM approvals"
                                   " WHERE id = {0}", (a2,))[0][0])
    chk("而且内容确实没被改掉", still.get("dsn") == GOOD)

print("\n=== 恢复票据不能被无 dsn 防重试误伤 ===\n")

import runs                                                   # noqa: E402

with open_store(readonly=False, init_schema=True) as st:
    resume_id = "resume-connect"
    resume_args = {"source_id": "resume-source", "dsn": GOOD}
    resume_hash = st.action_hash("connect_source", resume_args)
    resume_aid, _ = st.request(resume_id, resume_hash, "connect_source",
                                json.dumps(resume_args), "sponsor")
    st.decide(resume_aid, "approve", "boss@acme.com")
    st.append_event(resume_id, "SOURCE_CONNECT_FAILED", json.dumps(
        {"source_id": "resume-source",
         "identity": "ops_reader@127.0.0.1:5432/northwind",
         "error": "old password"}))
    runs.create("connect_source", resume_args, run_id=resume_id)
    runs.suspend(resume_id, resume_aid)
    with patch("datasteward_gate._source_not_granted", return_value=None):
        resumed = run_gate("connect_source", {"source_id": "resume-source"},
                           task_id="new-cron-session")
    chk("**恢复时隐藏 dsn 仍能消费当前已批准票**",
        resumed == {"action": "modify", "args": resume_args}, str(resumed))

print("\n=== 坑 6：做完的事不再发审批 ===\n")

with open_store(readonly=False, init_schema=True) as st:
    chk("没接过的表不拦",
        _already_done(st, "ingest_table",
                      {"source": "nw", "table": "never_touched"}) is None)
    sync.save_state("nw.orders", "full_refresh", row_count=830)
    m = _already_done(st, "ingest_table", {"source": "nw", "table": "orders"})
    chk("**刚接过的表被拦，且不建票**",
        bool(m) and "ALREADY_DONE" in m, str(m)[:80])
    chk("拦回的话里给了出路（要新数据用 full_refresh）",
        "full_refresh" in (m or "") and "sql_query" in (m or ""), str(m)[:120])
    chk("**`full_refresh` 自己不被拦**（它的语义就是明知有也要重来）",
        _already_done(st, "full_refresh",
                      {"source": "nw", "table": "orders"}) is None)

    # 时间窗：久到超出 RECENT_H 就该放行
    sync.save_state("nw.stale", "full_refresh", row_count=1)
    with open_store(readonly=False, init_schema=True) as w:
        w.db.execute("UPDATE sync_state SET last_synced_at=? WHERE asset=?",
                     (time.time() - 48 * 3600, "nw.stale"))
        w.db.commit()
    chk("**两天前接的不算「刚接过」**（不然表永远刷不了）",
        _already_done(st, "ingest_table",
                      {"source": "nw", "table": "stale"}) is None)

print("\n=== 坑 5：不认识的列类型不许蒙 ===\n")

chk("bytea → varbinary（不是 varchar）",
    sync._pg_type_to_trino("bytea") == "varbinary")
chk("smallint / integer 一律放宽成 bigint",
    sync._pg_type_to_trino("smallint") == sync._pg_type_to_trino("integer")
    == "bigint")
chk("character varying → varchar（Iceberg 不收长度）",
    sync._pg_type_to_trino("character varying") == "varchar")
chk("timestamp without time zone → timestamp(6)",
    sync._pg_type_to_trino("timestamp without time zone") == "timestamp(6)")
try:
    sync._pg_type_to_trino("geometry")
    chk("**不认识的类型抛错，不蒙 varchar**", False, "居然返回了一个类型")
except sync.UnknownColumnType as e:
    chk("**不认识的类型抛错，不蒙 varchar**", True)
    chk("报错里点名是哪个类型，并说了为什么不能蒙",
        "geometry" in str(e) and "插数" in str(e), str(e)[:90])
chk("它是 SyncError 的子类（既有的 except 接得住）",
    issubclass(sync.UnknownColumnType, sync.SyncError))

print("\n=== 坑 1：失败通知发给给账号的那个人 ===\n")

import importlib.util                                          # noqa: E402
import types                                                   # noqa: E402

_pkg = types.ModuleType("ds_retry_pkg")
_pkg.__path__ = [str(ROOT / ".hermes" / "plugins" / "data-steward")]
_pkg._ensure_path = lambda: None
sys.modules["ds_retry_pkg"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "ds_retry_pkg.tools", ROOT / ".hermes" / "plugins" / "data-steward" / "tools.py")
TOOLS = importlib.util.module_from_spec(_spec)
sys.modules["ds_retry_pkg.tools"] = TOOLS
_spec.loader.exec_module(TOOLS)

chk("**从自由文本里抠得出邮箱**（`given_by` 是模型转述的）",
    TOOLS._mail_of("dba@acme.com（sun 转交）") == "dba@acme.com",
    TOOLS._mail_of("dba@acme.com（sun 转交）"))
chk("抠不出来就返回空（不返回半截字符串当收件人）",
    TOOLS._mail_of("运维那边给的") == "")

out = pathlib.Path(os.environ["NOTIFY_OUTBOX"])
out.unlink(missing_ok=True)
TOOLS._notify_connect("northwind", "dba@acme.com（sun 转交）", [], "连不上：权限不足")
mails = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
         if l.strip()] if out.exists() else []
chk("**失败通知发给 dba**（给账号的那个人）",
    bool(mails) and mails[-1]["to"] == "dba@acme.com",
    str([m["to"] for m in mails]))

out.unlink(missing_ok=True)
TOOLS._notify_connect("northwind", "dba@acme.com", [("orders", 830)], "")
mails = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
         if l.strip()] if out.exists() else []
chk("**成功通知发给 owner**（先接哪几张表是业务判断）",
    bool(mails) and mails[-1]["to"] != "dba@acme.com",
    str([m["to"] for m in mails]))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
