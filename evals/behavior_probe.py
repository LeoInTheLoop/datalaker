"""行为 eval 的体外取证 —— 只查库看**实际状态**，不问被测系统。

与 `snapshot.py` 同一条理由（也同一条铁律）：判分要用的事实不能由被测
系统自报。区别在看什么：

    snapshot.py      源库 checksum / lake 产物  —— 「有没有动不该动的东西」
    behavior_probe   治理库全表 + 出站信         —— 「这一轮之后世界变成什么样」

行为 eval 不判路径（先调 A 再调 B 不作数），判的是**终态与副作用**，
所以取证面就是这两块：治理库里那几张写得下事实的表，加上真发出去的信。

只 import 标准库与 psycopg（同 `snapshot.py`：取证要够得着事实存放的地方，
而**判分器**那一份仍然是纯标准库 —— 判分必须能离线跑，取证不必）。
`evals/test_isolation.py` 断言这里一行被测代码都不许 import。

    python3 evals/behavior_probe.py --db /tmp/beh.db --outbox /tmp/beh.jsonl \
        --out /tmp/before.json
"""
import argparse
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 表 -> (SELECT 列, ORDER BY)。**顺序写死**：取证结果要能逐字节比对，
# 一处顺序抖动就会让 delta 里凭空冒出「新行」。
_TABLES = {
    "semantics": ("SELECT asset,key,value,confirmed_by,confirmed_at,source_item"
                  " FROM asset_semantics ORDER BY asset,key"),
    "catalog":   ("SELECT asset,kind,key,value,status,actor,observed_at"
                  " FROM asset_catalog WHERE superseded_by IS NULL"
                  " ORDER BY asset,kind,key,status"),
    "approvals": ("SELECT id,run_id,tool_name,approver,args_json,created_at,"
                  "expires_at,used_at,abandoned_at,kind,escalation_level,action_hash"
                  " FROM approvals ORDER BY created_at,id"),
    "decisions": ("SELECT approval_id,decision,chosen,approver,decided_at,token_jti"
                  " FROM decisions ORDER BY decided_at,approval_id"),
    "runs":      ("SELECT run_id,kind,status,waiting_on,note,owner_role,resumed,"
                  "params FROM runs ORDER BY created_at,run_id"),
    "sync":      ("SELECT asset,last_synced_at,row_count,schema_hash,last_error"
                  " FROM sync_state ORDER BY asset"),
    "provenance": ("SELECT asset,event,actor,approval_id,detail,ts"
                   " FROM asset_provenance ORDER BY ts,id"),
    "roles":     ("SELECT role,person,valid_from,valid_to,granted_by"
                  " FROM role_assignment ORDER BY role,valid_from,person"),
    "grants":    ("SELECT source_id,revealed_by FROM source_grants ORDER BY source_id"),
    "secrets":   ("SELECT source_id,approval_id,registered_by FROM source_secrets"
                  " ORDER BY source_id"),
    "events":    ("SELECT run_id,kind,payload,ts FROM events ORDER BY seq"),
}


# --------------------------------------------------------------------------
# `query_ledger` 不在治理库里 —— 它跟着 Connector 走
#
# `connector._persist` 只往 `.env` 里那个 **Postgres** DSN 写；SQLite 后端上
# 这张表**从来没人写**。取证却一直去 SQLite 里读它，于是「SQL 没执行」
# 这条判据读的是一张空表 —— 恒真。
#
# 这正是本项目反复摔的形状（闸门读的量根本没人写），而且是拆护栏对照
# 抓出来的：护栏都拆了，SQL 真跑出 5 行，判据还是绿的。
#
# 所以账本从它实际落地的地方读。时间边界用 **Postgres 自己的钟**
# （`now()`），不用本地时钟 —— 两边差几秒就会把上一个 case 的查询算进来。
# --------------------------------------------------------------------------
def _env(path=None) -> dict:
    d, p = {}, pathlib.Path(path or ROOT / ".env")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


def _ledger_dsn() -> str:
    e = _env()
    return e.get("STEWARD_AGENT_DSN") or e.get("DATASTEWARD_DSN", "")


def mark() -> dict:
    """取一个时间基准，供前后两次取证共用。**用账本那一侧的钟。**"""
    dsn = _ledger_dsn()
    if not dsn:
        return {"pg_ts": None, "dsn": ""}
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=10) as c, c.cursor() as cur:
            cur.execute("SELECT now()")
            return {"pg_ts": cur.fetchone()[0].isoformat(), "dsn": dsn}
    except Exception as e:                                    # noqa: BLE001
        return {"pg_ts": None, "dsn": "", "error": f"{type(e).__name__}: {e}"}


def queries(mk: dict) -> tuple:
    """基准之后的业务 SQL。没有 PG 账本时**如实返回「测不了」**，不返回空表。"""
    if not (mk or {}).get("dsn"):
        return [], "没有 Postgres 账本（SQLite 后端上 query_ledger 无人写）"
    try:
        import psycopg
        with psycopg.connect(mk["dsn"], connect_timeout=10) as c, c.cursor() as cur:
            cur.execute("SELECT ts, source_id, purpose, sql_text, rows_out,"
                        " est_rows, status FROM query_ledger"
                        " WHERE ts >= %s ORDER BY ts, id", (mk["pg_ts"],))
            cols = ("ts", "source_id", "purpose", "sql_text", "rows_out",
                    "est_rows", "status")
            # `est_rows` 是 numeric → psycopg 给 Decimal，json 序列化不了。
            # 取证结果要能落成文件给体外判分器吃，所以在这里就转成基本类型。
            return [dict(zip(cols, tuple(
                x if isinstance(x, (int, float, type(None))) else str(x)
                for x in r))) for r in cur.fetchall()], None
    except Exception as e:                                    # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"


def _rows(con, sql):
    """查一张表。**表不存在不是致命错**，但要如实记下来。"""
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()], None
    except sqlite3.Error as e:
        return [], f"{type(e).__name__}: {e}"


def governance(db_path, mk=None) -> dict:
    """治理库现状。**只读连接**，取证不许改变被取证的东西。"""
    out = {"_db": str(db_path), "_errors": {}}
    p = pathlib.Path(db_path or "")
    if not p.exists():
        out["_errors"]["__db__"] = f"治理库不存在：{p}"
        return {**out, **{k: [] for k in _TABLES}, "queries": []}
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        for name, sql in _TABLES.items():
            out[name], err = _rows(con, sql)
            if err:
                out["_errors"][name] = err
    finally:
        con.close()
    out["queries"], err = queries(mk or {})
    if err:
        out["_errors"]["queries"] = err
    return out


def mails(outbox_path) -> tuple:
    """真发出去的信（outbox 通道的 JSONL）。**发给谁、几封**是副作用。

    **文件不在 ≠ 一封没发。** 也可能是 NOTIFY_CHANNEL 没走 outbox、
    路径配错了 —— 那时「没发错人」这条判据判的是空气。所以如实报出来，
    由判分器判成 MISSING_EVIDENCE。注入器会在起点建一个空文件，
    「存在但是空的」才是「没发信」。
    """
    p = pathlib.Path(outbox_path or "")
    if not p.exists():
        return [], f"收件记录不存在：{p}（通道没走 outbox？）"
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass                      # 半行：跑到一半被杀过，不算证据
    return out, None


def take(db_path, outbox_path, mk=None) -> dict:
    gov = governance(db_path, mk)
    box, err = mails(outbox_path)
    if err:
        gov["_errors"]["mails"] = err
    return {"governance": gov, "mails": box}


# --------------------------------------------------------------------------
# delta：这一轮**新增**了什么
#
# 行为 eval 判的是「这一轮之后」，而起点是注入出来的（本来就有票、有信、
# 有挂起的线）。不减去起点的话，注入的那 3 封信会被当成 Agent 这轮发的，
# 「重复发信」这一条永远红。
# --------------------------------------------------------------------------
_KEYS = {
    "semantics": ("asset", "key", "value"),
    "catalog":   ("asset", "kind", "key", "status", "value"),
    "approvals": ("id",),
    "decisions": ("approval_id", "decision", "token_jti"),
    "runs":      ("run_id", "status", "waiting_on"),
    "sync":      ("asset", "last_synced_at", "row_count"),
    "provenance": ("asset", "event", "ts"),
    "roles":     ("role", "person", "valid_from", "valid_to"),
    "grants":    ("source_id",),
    "secrets":   ("source_id",),
    "queries":   ("sql_text", "ts"),
    "events":    ("kind", "payload", "ts"),
}


def _sig(row, keys):
    return tuple(str(row.get(k)) for k in keys)


def delta(before: dict, after: dict) -> dict:
    """after 里有、before 里没有的行。按主键签名比，不按整行 —— 
    `runs.updated_at` 这类每轮都变的字段会让整行比对全是「新行」。"""
    out = {}
    for name, keys in _KEYS.items():
        seen = {_sig(r, keys) for r in before.get("governance", {}).get(name, [])}
        out[name] = [r for r in after.get("governance", {}).get(name, [])
                     if _sig(r, keys) not in seen]
    n = len(before.get("mails", []))
    out["mails"] = after.get("mails", [])[n:]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--outbox", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    snap = take(a.db, a.outbox, mark())
    text = json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True)
    if a.out:
        pathlib.Path(a.out).write_text(text, encoding="utf-8")
        print(f"取证已写入 {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
