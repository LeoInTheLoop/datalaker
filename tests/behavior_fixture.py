"""行为 eval 的**状态注入** —— 直接把起点造出来，不从「发现表」跑一遍。

为什么不跑完整流程：这个 Agent 的一条线可能等人两天、发三封信、中途
换过负责人。要测「它在这种局面下的下一步」，从头跑一遍既慢又不可控 ——
而且真正容易出错的局面（过期票 + 重复 callback + 新邮件跟旧口径打架）
按流程根本不好凑。**直接构造状态**才测得到那种局面。

这个文件是**体内**的（它 import 被测代码），理由与 `tests/run_eval_case.py`
一样：布置考场不涉及公正性，而算动作指纹必须用被测系统自己那份函数 ——
在体外重写一份，注入出来的「有效票」跟门禁认的就不是同一张。
判分与取证仍在体外（`evals/score_behavior.py` / `evals/behavior_probe.py`）。

state 的词汇是有限的一小组，全在 `_WRITERS` 里。

「直接构造状态」这件事本身是对的 —— 与 Anthropic `commerce-agents` 的
snapshot eval 规范同形（构造状态 → 一条消息 → 判终态与最后一次写的参数）。
TODO(R7): 缺的是**导出**方向 —— 真模型跑出一个值得锁住的局面时，只能人照着
库里的行往 JSON 里抄。加 dump：按同一组词汇反向导出 `state`，认不出的字段
直接报错不猜，凭证按 `_CREDENTIAL_KEYS` 剔掉，时间导成 `age_h` 相对量。
这是省人力，不是让这层 eval 成立的前提（readme R7 P2）。
"""
import json
import os
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

H = 3600.0


def _now():
    return time.time()


def _ago(hours):
    """`age_h: 48` = 48 小时前。挂起两天这类局面全靠它。"""
    return _now() - float(hours or 0) * H


# --------------------------------------------------------------------------
# 每种 state 词汇一个写入函数。**用原始 SQL 写**，不走被测系统的写路径：
#
#   1. 有些起点是被测系统自己造不出来的 —— 过期票、已消费的票、
#      两天前发过的信。走它的写路径就得先把时间调回去。
#   2. 建表 DDL 仍然用它的（`open_store`），所以字段一改这里就会
#      **当场报错**而不是静默写歪。宁可炸，不可歪。
# --------------------------------------------------------------------------
def _sources(db, rows, ids):
    for r in rows:
        db.execute("INSERT OR REPLACE INTO source_grants"
                   " (source_id, revealed_by, revealed_at, note) VALUES (?,?,?,?)",
                   (r["source_id"], r.get("revealed_by", "boss@acme.com"),
                    _ago(r.get("age_h", 72)), r.get("note", "行为 eval 注入")))


def _secrets(db, rows, ids):
    for r in rows:
        db.execute("INSERT OR REPLACE INTO source_secrets"
                   " (source_id, dsn, kind, approval_id, registered_by, registered_at)"
                   " VALUES (?,?,?,?,?,?)",
                   (r["source_id"], r.get("dsn", "postgresql://x/y"), "postgres",
                    ids.get(r.get("ticket"), r.get("ticket", "injected")),
                    r.get("by", "boss@acme.com"), _ago(r.get("age_h", 48))))


def _roles(db, rows, ids):
    """角色分配。`valid_to_h` 给了就是**已经卸任的人** —— 换过负责人。"""
    for r in rows:
        vt = None if r.get("valid_to_h") is None else _ago(r["valid_to_h"])
        db.execute("INSERT INTO role_assignment"
                   " (role, person, valid_from, valid_to, granted_by, reason)"
                   " VALUES (?,?,?,?,?,?)",
                   (r["role"], r["person"], _ago(r.get("valid_from_h", 240)), vt,
                    r.get("granted_by", "boss@acme.com"), r.get("reason", "注入")))


def _semantics(db, rows, ids):
    for r in rows:
        db.execute("INSERT OR REPLACE INTO asset_semantics"
                   " (asset, key, value, confirmed_by, confirmed_at, source_item)"
                   " VALUES (?,?,?,?,?,?)",
                   (r["asset"], r["key"], r["value"], r["by"],
                    _ago(r.get("age_h", 24)), r.get("source_item")))


def _catalog(db, rows, ids):
    for r in rows:
        db.execute("INSERT INTO asset_catalog"
                   " (asset, kind, key, value, status, evidence, actor,"
                   "  observed_at, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                   (r["asset"], r["kind"], r["key"],
                    r["value"] if isinstance(r["value"], str)
                    else json.dumps(r["value"], ensure_ascii=False),
                    r.get("status", "inferred"),
                    json.dumps(r.get("evidence") or {}, ensure_ascii=False),
                    r.get("actor", "system:injected"), _ago(r.get("age_h", 24)),
                    uuid.uuid4().hex[:16]))


def _tickets(db, rows, ids):
    """审批票 + 可选的决定。

    `decision` 给了就同时写一行 `decisions` —— 那是**人**在这一轮之前
    做的决定，注入它是合法的（判分比的是增量，见 `behavior_probe.delta`）。
    Agent 这一轮再多写一条，`decisions_unchanged` 就红。

    `expired: true` / `used: true` 造的是「看着像有票，其实用不了」——
    最容易被当成有效授权的两种残留。
    """
    from datasteward_gate.approvals import action_hash
    for r in rows:
        aid = str(uuid.uuid4())
        ids[r["id"]] = aid
        created = _ago(r.get("age_h", 24))
        ttl = 72 * H
        exp = created - 1 if r.get("expired") else created + ttl
        db.execute("INSERT INTO approvals (id, run_id, action_hash, tool_name,"
                   " args_json, approver, created_at, expires_at, used_at, kind,"
                   " escalation_level, question, options)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (aid, ids.get(r.get("run"), r.get("run", "injected")),
                    action_hash(r["tool"], r.get("args") or {}), r["tool"],
                    json.dumps(r.get("args") or {}, ensure_ascii=False),
                    r.get("approver", "steward"), created, exp,
                    created + 60 if r.get("used") else None,
                    r.get("kind", "approval"), int(r.get("escalation_level", 0)),
                    r.get("question"), json.dumps(r["options"], ensure_ascii=False)
                    if r.get("options") else None))
        if r.get("decision"):
            db.execute("INSERT INTO decisions (id, approval_id, decision, chosen,"
                       " approver, decided_at, token_jti, message_id)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), aid, r["decision"], r.get("chosen"),
                        r.get("decided_by", "wang@acme.com"),
                        created + 120, uuid.uuid4().hex, None))


def _runs(db, rows, ids):
    """任务线。`status=waiting_human` + `waiting_on` = 挂在某张票上。"""
    for r in rows:
        rid = r.get("run_id") or str(uuid.uuid4())
        ids[r["id"]] = rid
        created = _ago(r.get("age_h", 48))
        db.execute("INSERT INTO runs (run_id, kind, params, status, waiting_on,"
                   " checkpoint, note, owner_role, created_at, updated_at, resumed)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, r["kind"], json.dumps(r.get("params") or {}, ensure_ascii=False),
                    r.get("status", "waiting_human"),
                    ids.get(r.get("waiting_on"), r.get("waiting_on")),
                    json.dumps(r.get("checkpoint") or {}, ensure_ascii=False),
                    r.get("note", ""), r.get("owner_role"), created,
                    _ago(r.get("updated_h", r.get("age_h", 48))),
                    int(r.get("resumed", 0))))


def _sync(db, rows, ids):
    for r in rows:
        db.execute("INSERT OR REPLACE INTO sync_state (asset, strategy, watermark,"
                   " last_synced_at, data_as_of, freshness_sla_h, schema_hash,"
                   " row_count, last_error) VALUES (?,?,?,?,?,?,?,?,?)",
                   (r["asset"], r.get("strategy", "full"), None,
                    _ago(r.get("age_h", 12)), _ago(r.get("age_h", 12)), 24,
                    r.get("schema_hash", "h0"), r.get("rows", 0), None))


def _provenance(db, rows, ids):
    for r in rows:
        db.execute("INSERT INTO asset_provenance (asset, event, actor, approval_id,"
                   " detail, ts) VALUES (?,?,?,?,?,?)",
                   (r["asset"], r["event"], r.get("actor", "claw"),
                    ids.get(r.get("ticket"), r.get("ticket")),
                    json.dumps(r.get("detail") or {}, ensure_ascii=False),
                    _ago(r.get("age_h", 24))))


def _stage(db, rows, ids):
    """阶段提案 + 人选的那个选项（轮开没开）。

    **形状必须跟 `Store.ask()` 写出来的一模一样** —— 门禁认的是
    `kind='question' AND args_json LIKE '%__stage__%'`，不是工具名。
    第一版这里写的是 `tool_name='propose_stage_decision'` + `args_json='{}'`，
    于是 `stage_proposals()` 数出 0、轮制判成「没启用」，
    `apply_cleaning_rule` 被后面的「没票」挡住而不是被轮门挡住 ——
    case 照样绿，但**绿的理由不是要测的那条**。是 evidence.blocked_codes
    里那个 PENDING_APPROVAL 把它露出来的。
    """
    from datasteward_gate.approvals import action_hash
    for r in rows if isinstance(rows, list) else [rows]:
        aid = str(uuid.uuid4())
        created = _ago(r.get("age_h", 6))
        q = r.get("question", "第一周小结：下一步三选一")
        db.execute("INSERT INTO approvals (id, run_id, action_hash, tool_name,"
                   " args_json, approver, created_at, expires_at, kind, question)"
                   " VALUES (?,?,?,?,?,?,?,?,'question',?)",
                   (aid, "round", action_hash("__question__",
                                              {"asset": "__stage__", "q": q}),
                    "__question__", json.dumps({"asset": "__stage__"},
                                               ensure_ascii=False),
                    r.get("approver", "sponsor"), created, created + 72 * H, q))
        if r.get("chosen"):
            db.execute("INSERT INTO decisions (id, approval_id, decision, chosen,"
                       " approver, decided_at, token_jti) VALUES (?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), aid, "answered", r["chosen"],
                        r.get("decided_by", "boss@acme.com"), created + 60,
                        uuid.uuid4().hex))


def _events(db, rows, ids):
    for r in rows:
        db.execute("INSERT INTO events (run_id, ts, kind, payload) VALUES (?,?,?,?)",
                   (ids.get(r.get("run"), r.get("run", "injected")),
                    _ago(r.get("age_h", 24)), r["kind"], r.get("payload", "")))


_WRITERS = {
    "sources": _sources, "secrets": _secrets, "roles": _roles,
    "semantics": _semantics, "catalog": _catalog, "tickets": _tickets,
    "runs": _runs, "sync": _sync, "provenance": _provenance,
    "stage": _stage, "events": _events,
}

# 注入顺序有依赖：线要先于票（票的 run_id 指线），票要先于凭证与台账。
#
# **但这里有个环**：票的 `run_id` 指线，线的 `waiting_on` 又指票。
# 先写线的时候票还没有 id，`ids.get("t1", "t1")` 就落回字面量 —— 于是
# `runs.waiting_on = "t1"`，`runs.resumable()` 连不上那张票，返回空。
# 后果不是报错，是 **monitor 输出为空、模型收到一个没有内容的唤醒**，
# 而它非常正确地拒绝去猜。gate 档发现不了（它不走 monitor），
# live 档第一次跑就撞出来。解法是写完票之后**回填一遍**。
_ORDER = ["sources", "roles", "runs", "tickets", "secrets", "semantics",
          "catalog", "sync", "provenance", "stage", "events"]


def _resolve_waiting_on(db, rows, ids):
    """票写完之后，把线上的 `waiting_on` 从局部 id 换成真的 approval id。"""
    for r in rows or []:
        want = r.get("waiting_on")
        if want and want in ids:
            db.execute("UPDATE runs SET waiting_on=? WHERE run_id=?",
                       (ids[want], ids[r["id"]]))


def _mails(path, rows, ids):
    """**已经发过的信**也要注入 —— 「这一轮又发了几封」才有分母。

    挂起两天、发过三封信的局面，少了这一段就退化成「第一次问人」，
    而重复打扰这一类缺陷正好只在第二次之后才出现。
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for r in rows:
            rec = {"ts": _ago(r.get("age_h", 24)), "kind": r.get("kind", "approval"),
                   "to": r["to"], **{k: v for k, v in r.items()
                                     if k not in ("to", "kind", "age_h")}}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def build(case: dict, db_path: str, outbox_path: str) -> dict:
    """把 case 的 `state` 造进库里，返回 局部 id -> 真实 id 的映射。

    库先删干净 —— 残留是这个项目最会骗人的东西（见 `reset_live.py`）。
    """
    for suf in ("", "-wal", "-shm"):
        pathlib.Path(db_path + suf).unlink(missing_ok=True)
    # **建一个空文件，不是不建。** 「文件不在」要留给「通道配错了」这个
    # 含义用；「存在但是空的」才是「这一轮一封信都没发」。
    box = pathlib.Path(outbox_path)
    box.unlink(missing_ok=True)
    box.parent.mkdir(parents=True, exist_ok=True)
    box.touch()
    os.environ["DATASTEWARD_DB"] = db_path
    os.environ.pop("DATASTEWARD_DSN", None)

    from datasteward_gate.approvals import Store
    st = Store(db_path, readonly=False)          # 建表用被测系统自己的 DDL
    ids: dict = {}
    state = case.get("state") or {}
    try:
        for name in _ORDER:
            rows = state.get(name)
            if rows:
                _WRITERS[name](st.db, rows, ids)
        _resolve_waiting_on(st.db, state.get("runs"), ids)
        st.db.commit()
    finally:
        st.close()
    if state.get("mails"):
        _mails(outbox_path, state["mails"], ids)
    return ids
