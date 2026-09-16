"""Snapshot 起点还原器 —— 独立进程、独立账号、自验后才放行。

**为什么是独立的一个文件和一个进程，而不是 demo_ui 里的一个函数。**

`decisions` 的合法写入方只有 approval callback（`approver_role` 是唯一有
`INSERT ON decisions` 的角色）。观察器用超级用户也能写得进去，但那样一来
「谁能写决定」在代码层面就糊了：读代码的人分不清这是夹具在还原历史，
还是适配器获得了写决定的能力。`tests/test_demo_ui.py` 里那条
`assertNotIn("INSERT INTO decisions", demo_ui 源码)` 守的就是这个属性 ——
把还原逻辑塞回 demo_ui 只有两条路：削弱那条断言，或者绕开字符串检查。
两条都是拿测试换方便。

所以分工是：

    观察器          决定「跑哪个 Snapshot」，把 state 交出去，等还原结果
    还原器（本文件） 清空 → 写起点 → **自验** → 交回清单，然后退出
    callback        运行期间新产生的决定，仍然只由它写
    网关 / cron     还原完成之后才启动

**历史决定要能恢复，但分三段看**（docs/snapshot-testing.md）：恢复前由这里
还原并记录来源；运行中新决定只能由 callback 产生；判分时历史决定属于基线，
不能算成本轮新完成的审批。`runs.resumable()` 是 `runs JOIN decisions`，
没有历史决定行，「callback 已写决定、cron 尚未恢复」这个起点根本构造不出来。

**时间是相对量。** case 文件里写 `age_h`（多少小时前）、`expires_h`（多少小时后
过期），落库时才换算成绝对时刻。写死时间戳的 case 放一周就全部过期，
而「票还有两小时到期」「这条线挂了三十小时」正是恢复类测例要表达的东西。

**两种时间列。** `approvals` / `decisions` 用 `timestamptz`，
`runs` / `source_contacts` / `role_assignment` 用 `double precision` epoch。
`tests/behavior_fixture.py` 是纯 SQLite、一律数字，照搬到这里会炸 ——
这也是「便宜层有这些字段」不等于真链路有还原实现的一个具体形态。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import uuid
import math
from contextvars import ContextVar
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]
_ANCHOR = ContextVar("snapshot_restore_anchor", default=None)


class RestoreFailed(RuntimeError):
    """起点没能还原成声明的样子。**绝不降级成空状态继续跑。**"""


def _dsn() -> str:
    value = os.environ.get("SNAPSHOT_RESTORE_DSN") or os.environ.get("DEMO_ADMIN_DSN") or ""
    if not value:
        raise RestoreFailed("缺少 SNAPSHOT_RESTORE_DSN / DEMO_ADMIN_DSN，无法还原起点")
    return value


def _connect():
    import psycopg
    # **不 autocommit。** 还原要么整份成立，要么一行都不留：半份起点
    # （票据写进去了、决定没写、waiting_on 没回填）比没有起点更糟 ——
    # 它能跑，跑出来的结论却说不清是从哪开始的。
    return psycopg.connect(_dsn(), connect_timeout=10)


def _fingerprint(tool: str, args: dict) -> str:
    """用**生产那一份**指纹函数，不是自己拼一个。

    门禁靠 `action_hash` 把「模型现在要做的事」和「人批过的那张票」对上。
    还原器写 `restored:<ticket_id>` 这种自造值，票据在库里看着齐全，
    真到门禁那里一条都匹配不上 —— 起点看起来有审批，实际等于没有。
    """
    from datasteward_gate.approvals import action_hash
    return action_hash(tool, args or {})


# 每一类起点允许出现的字段。**未知字段一律拒绝**，不因为调用入口不同而放宽：
# 绕过网页直接调还原器，同样不能静默跳过一个写错的 key。
FIELDS = {
    "roles": {"role", "person", "age_h", "ended_h", "granted_by"},
    "contacts": {"source_id", "email", "display_name", "relationship",
                 "recorded_from", "age_h"},
    "runs": {"id", "kind", "status", "params", "checkpoint", "note", "owner_role",
             "age_h", "updated_h", "next_action_h", "resumed", "waiting_on"},
    "tickets": {"id", "run", "tool", "args", "approver", "age_h", "expires_h",
                "used_h", "kind", "escalation_level", "waits", "action_hash",
                "decision", "decided_by", "decided_h", "question", "options", "evidence",
                "abandoned_h", "chosen"},
    "catalog": {"asset", "kind", "key", "value", "status", "evidence", "actor", "age_h"},
    "patches": {"table", "key", "set", "expect"},
}
REQUIRED = {"roles": ("role", "person"), "contacts": ("source_id", "email"),
            "runs": ("id", "kind"), "tickets": ("id", "tool"),
            "catalog": ("asset", "kind", "key"), "patches": ("table", "key", "set", "expect")}
PATCH_TABLES = {"approvals", "decisions", "role_assignment", "source_contacts", "runs",
                "source_grants", "source_secrets", "asset_catalog", "asset_semantics",
                "sync_state", "asset_provenance", "events", "mail_threads",
                "remediation_ledger", "cleaning_rules", "query_ledger", "usage_ledger"}


def check_state(state: dict) -> None:
    """声明检查，在碰数据库之前。"""
    if not isinstance(state, dict):
        raise RestoreFailed("state 必须是对象")
    for key, rows in state.items():
        if key not in FIELDS:
            raise RestoreFailed(
                f"state.{key} 还原器不支持；当前支持：{'、'.join(sorted(FIELDS))}")
        if not isinstance(rows, list):
            raise RestoreFailed(f"state.{key} 必须是数组")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RestoreFailed(f"state.{key}[{index}] 必须是对象")
            unknown = set(row) - FIELDS[key]
            if unknown:
                raise RestoreFailed(
                    f"state.{key}[{index}] 不认识的字段：{'、'.join(sorted(unknown))}；"
                    f"可用：{'、'.join(sorted(FIELDS[key]))}")
            missing = [f for f in REQUIRED[key] if not str(row.get(f) or "").strip()]
            if missing:
                raise RestoreFailed(
                    f"state.{key}[{index}] 缺必填字段：{'、'.join(missing)}")
            for field, value in row.items():
                if field.endswith("_h") and value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise RestoreFailed(f"state.{key}[{index}].{field} 必须是有限数字")
            if key == "patches":
                if row["table"] not in PATCH_TABLES:
                    raise RestoreFailed(f"不支持覆盖表 {row['table']}")
                if any(not isinstance(row[field], dict) or not row[field] for field in ("key", "set", "expect")):
                    raise RestoreFailed("patch key/set/expect 必须是非空对象")
                if set(row["set"]) - set(row["expect"]):
                    raise RestoreFailed("patch 必须声明每个修改字段的 expect 原值")
            if key == "tickets" and row.get("decision") not in (None, "approve", "deny", "answered"):
                raise RestoreFailed("票据 decision 只支持 approve/deny/answered")
            if key in ("runs", "tickets"):
                if any(other.get("id") == row["id"] for other in rows[:index]):
                    raise RestoreFailed(f"重复的 {key} id: {row['id']}")
    # 票据引用的任务线必须真的声明过，否则 waiting_on 回填会指向不存在的线。
    declared = {row["id"] for row in state.get("runs") or []}
    for row in state.get("tickets") or []:
        target = row.get("run")
        if target and target not in declared:
            raise RestoreFailed(f"票据 {row['id']} 引用了未声明的任务线 {target}")
        if row.get("waits") and not target:
            raise RestoreFailed(f"票据 {row['id']} 声明了 waits 却没有 run")
    for row in state.get("catalog", []):
        if "value" not in row or row.get("status", "inferred") not in ("observed", "inferred", "confirmed", "refuted"):
            raise RestoreFailed("catalog 必须声明 value 和合法 status")
    touched = {TABLES[k] for k, rows in state.items() if rows and k in TABLES}
    if any(row["table"] in touched for row in state.get("patches", [])):
        raise RestoreFailed("同一张表不能同时使用声明式替换和 patches，避免隐含覆盖顺序")


def _exec(cur, sql: str, params=()) -> None:
    cur.execute(sql, params)


def _ago(hours) -> float:
    """`age_h` 小时之前的绝对时刻。"""
    return (_ANCHOR.get() or time.time()) - float(hours or 0) * 3600.0


def _ahead(hours) -> float:
    """`expires_h` 小时之后的绝对时刻。负数表示已经过期。"""
    return (_ANCHOR.get() or time.time()) + float(hours or 0) * 3600.0


# ---------------------------------------------------------------- 各平面写入
def _roles(cur, rows, ids) -> list[str]:
    # Explicit replacement of the declared role's history. Otherwise a newer
    # bootstrap assignment wins resolve_role(), despite the injected row existing.
    for role in sorted({row["role"] for row in rows}):
        _exec(cur, "DELETE FROM role_assignment WHERE role=%s", (role,))
    for row in rows:
        _exec(cur,
              "INSERT INTO role_assignment (role, person, valid_from, valid_to,"
              " granted_by, reason) VALUES (%s,%s,%s,%s,%s,%s)",
              (row["role"], row["person"], _ago(row.get("age_h", 720)),
               _ago(row["ended_h"]) if row.get("ended_h") is not None else None,
               row.get("granted_by", "boss@acme.com"), "Snapshot 起点还原"))
    return [f"角色 {r['role']}→{r['person']}" for r in rows]


def _contacts(cur, rows, ids) -> list[str]:
    for row in rows:
        _exec(cur,
              "INSERT INTO source_contacts (source_id, email, display_name,"
              " relationship, recorded_from, recorded_at)"
              " VALUES (%s,%s,%s,%s,%s,%s)"
              " ON CONFLICT (source_id, email) DO UPDATE SET display_name=EXCLUDED.display_name,"
              " relationship=EXCLUDED.relationship, recorded_from=EXCLUDED.recorded_from,"
              " recorded_at=EXCLUDED.recorded_at",
              (row["source_id"], row["email"], row.get("display_name", row["email"]),
               row.get("relationship", "技术联系人"),
               row.get("recorded_from", "Snapshot 起点还原"), _ago(row.get("age_h", 24))))
    return [f"联系人 {r['source_id']}→{r['email']}" for r in rows]


def _runs(cur, rows, ids) -> list[str]:
    for row in rows:
        run_id = ids.setdefault(("run", row["id"]), row["id"])
        next_at = row.get("next_action_h")
        _exec(cur,
              "INSERT INTO runs (run_id, kind, params, status, waiting_on, checkpoint,"
              " note, owner_role, created_at, updated_at, resumed, next_action_at)"
              " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
              " ON CONFLICT (run_id) DO UPDATE SET kind=EXCLUDED.kind, params=EXCLUDED.params,"
              " status=EXCLUDED.status, waiting_on=EXCLUDED.waiting_on, checkpoint=EXCLUDED.checkpoint,"
              " note=EXCLUDED.note, owner_role=EXCLUDED.owner_role, created_at=EXCLUDED.created_at,"
              " updated_at=EXCLUDED.updated_at, resumed=EXCLUDED.resumed, next_action_at=EXCLUDED.next_action_at",
              (run_id, row["kind"],
               json.dumps(row.get("params") or {}, ensure_ascii=False),
               row.get("status", "waiting_human"),
               row.get("waiting_on"),
               json.dumps(row.get("checkpoint") or {}, ensure_ascii=False),
               row.get("note", "Snapshot 起点还原"), row.get("owner_role"),
               _ago(row.get("age_h", 24)), _ago(row.get("updated_h", row.get("age_h", 24))),
               int(row.get("resumed", 0)), _ahead(next_at) if next_at is not None else None))
    return [f"任务线 {r['id']}（{r['kind']}/{r.get('status', 'waiting_human')}）" for r in rows]


def _tickets(cur, rows, ids) -> list[str]:
    """票据，以及**已经发生过的**决定。

    `waiting_on` 在这里回填：票据 id 是还原时才生成的，case 里写的是局部名。
    """
    out = []
    for row in rows:
        ticket_id = ids.setdefault(("ticket", row["id"]), str(uuid.uuid4()))
        run_local = row.get("run")
        run_id = ids.get(("run", run_local), run_local or "restored-orphan")
        created = _ago(row.get("age_h", 24))
        used = row.get("used_h")
        _exec(cur,
              "INSERT INTO approvals (id, run_id, action_hash, tool_name, args_json,"
              " approver, created_at, expires_at, used_at, kind, escalation_level)"
              " VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),"
              "         NULL,%s,%s)",
              (ticket_id, run_id,
               row.get("action_hash") or _fingerprint(row["tool"], row.get("args")),
               row["tool"], json.dumps(row.get("args") or {}, ensure_ascii=False),
               row.get("approver", "steward"), created,
               _ahead(row.get("expires_h", 72)),
               row.get("kind", "approval"), int(row.get("escalation_level", 0))))
        # `used_at` 单独一步：列是 timestamptz，只有走 to_timestamp 才对得上。
        if used is not None:
            _exec(cur, "UPDATE approvals SET used_at = to_timestamp(%s) WHERE id = %s",
                  (_ago(used), ticket_id))
        _exec(cur, "UPDATE approvals SET question=%s, options=%s, evidence=%s,"
              " abandoned_at=to_timestamp(%s) WHERE id=%s",
              (row.get("question"), json.dumps(row["options"], ensure_ascii=False) if "options" in row else None,
               json.dumps(row["evidence"], ensure_ascii=False) if "evidence" in row else None,
               _ago(row["abandoned_h"]) if row.get("abandoned_h") is not None else None, ticket_id))
        out.append(f"票据 {row['id']}（{row['tool']}）")

        decision = row.get("decision")
        if decision:
            # 历史决定：恢复前由还原器写，运行中仍只有 callback 能写。
            _exec(cur,
                  "INSERT INTO decisions (id, approval_id, decision, approver,"
                  " decided_at, token_jti, chosen) VALUES (%s,%s,%s,%s,to_timestamp(%s),%s,%s)",
                  (str(uuid.uuid4()), ticket_id, decision,
                   row.get("decided_by", "alice@acme.com"),
                   _ago(row.get("decided_h", row.get("age_h", 24))),
                   f"restored-{uuid.uuid4().hex}", row.get("chosen")))
            out.append(f"决定 {row['id']}={decision}（基线，非本轮）")
        if run_local and row.get("waits"):
            _exec(cur, "UPDATE runs SET waiting_on = %s WHERE run_id = %s",
                  (ticket_id, ids.get(("run", run_local), run_local)))
    return out


def _catalog(cur, rows, ids):
    from datasteward_gate.approvals import _catalog_norm
    for row in rows:
        value, evidence, fingerprint = _catalog_norm(row["value"], row.get("evidence"), None)
        cur.execute("INSERT INTO asset_catalog (asset,kind,key,value,status,evidence,actor,observed_at,fingerprint)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (row["asset"], row["kind"], row["key"], value, row.get("status", "inferred"),
                     evidence, row.get("actor", "snapshot:derived"), _ago(row.get("age_h", 0)), fingerprint))
        new_id = cur.fetchone()[0]
        cur.execute("UPDATE asset_catalog SET superseded_by=%s WHERE asset=%s AND kind=%s AND key=%s"
                    " AND superseded_by IS NULL AND id<>%s",
                    (new_id, row["asset"], row["kind"], row["key"], new_id))
    return [f"档案 {row['asset']}/{row['kind']}/{row['key']}" for row in rows]


def _json_value(value):
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)))


def _patches(cur, rows, ids):
    from psycopg import sql
    from psycopg.types.json import Jsonb
    for row in rows:
        cur.execute("SELECT column_name,data_type FROM information_schema.columns"
                    " WHERE table_schema='public' AND table_name=%s", (row["table"],))
        columns = dict(cur.fetchall())
        if (set(row["key"]) | set(row["set"]) | set(row["expect"])) - columns.keys():
            raise RestoreFailed(f"patch 存在未知列：{row['table']}")
        def adapt(key, value):
            if isinstance(value, dict) and set(value) == {"hours_from_restore"}:
                value = _ahead(value["hours_from_restore"])
                if columns[key].startswith("timestamp"):
                    value = datetime.fromtimestamp(value, timezone.utc)
                elif columns[key] not in ("double precision", "real", "numeric"):
                    raise RestoreFailed(f"相对时间用于非时间列：{key}")
            return Jsonb(value) if columns[key] in ("json", "jsonb") else value
        where = sql.SQL(" AND ").join(sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(k)) for k in row["key"])
        query = sql.SQL("SELECT * FROM {} WHERE ").format(sql.Identifier(row["table"])) + where
        params = [adapt(k, v) for k, v in row["key"].items()]
        cur.execute(query, params)
        found = cur.fetchall()
        if len(found) != 1:
            raise RestoreFailed(f"patch 必须恰好匹配一条基线记录：{row['table']}，实际 {len(found)}")
        before = dict(zip([c.name for c in cur.description], found[0]))
        if any(_json_value(before[k]) != v for k, v in row["expect"].items()):
            raise RestoreFailed(f"patch expect 与基线不符：{row['table']}")
        assignments = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in row["set"])
        cur.execute(sql.SQL("UPDATE {} SET ").format(sql.Identifier(row["table"])) + assignments + sql.SQL(" WHERE ") + where,
                    [adapt(k, v) for k, v in row["set"].items()] + params)
        if cur.rowcount != 1:
            raise RestoreFailed("patch 修改行数异常")
        cur.execute(query, params)
        changed = cur.fetchone()
        if changed is None:
            raise RestoreFailed("patch 不允许改变定位该记录的 key")
        actual = dict(zip([c.name for c in cur.description], changed))
        for key, value in row["set"].items():
            expected = adapt(key, value)
            if isinstance(expected, Jsonb):
                expected = expected.obj
            if _json_value(actual[key]) != _json_value(expected):
                raise RestoreFailed(f"patch 写后读回不符：{row['table']}.{key}")
    return [f"覆盖 {r['table']} {r['key']}" for r in rows]


WRITERS = [("roles", _roles), ("contacts", _contacts), ("runs", _runs),
           ("tickets", _tickets), ("catalog", _catalog), ("patches", _patches)]
TABLES = {"roles": "role_assignment", "contacts": "source_contacts", "runs": "runs",
          "tickets": "approvals", "catalog": "asset_catalog"}


def _agent_view(cur):
    """Use the actual production read implementations on the uncommitted state."""
    from datasteward_gate.approvals import PgStore
    from identity import resolve_to
    view = PgStore.__new__(PgStore)
    view.db, view.readonly = cur.connection, True
    return view, resolve_to


# ---------------------------------------------------------------- 起点自验
def verify(cur, state: dict, ids: dict) -> list[dict]:
    """**声明了什么，就去库里查出什么。**

    还原不报错不等于起点对：行数对不上、引用连不通、票据其实已过期，
    这三种都能让一次 run 从一个不是声明的起点跑起来，而页面和判分全程正常。
    自验失败一律抛 —— 宁可这一轮不开始，也不要产出一个说不清起点的结论。
    """
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise RestoreFailed(f"起点自验未通过：{name} —— {detail}")

    for row in state.get("roles") or []:
        cur.execute("SELECT count(*) FROM role_assignment WHERE role=%s AND person=%s",
                    (row["role"], row["person"]))
        check(f"角色记录 {row['role']}", cur.fetchone()[0] >= 1, f"{row['person']}")

    view, resolve_to = _agent_view(cur)
    for role in {r["role"] for r in state.get("roles", [])}:
        current = [r for r in state["roles"] if r["role"] == role and r.get("ended_h") is None]
        if current:
            expected = min(current, key=lambda r: r.get("age_h", 720))["person"]
            actual = resolve_to(view, role)
            check(f"Agent 解析角色 {role}", actual == expected, f"实际={actual}；声明={expected}")

    for row in state.get("contacts") or []:
        cur.execute("SELECT display_name,relationship,recorded_from FROM source_contacts WHERE source_id=%s AND lower(email)=lower(%s)",
                    (row["source_id"], row["email"]))
        got = cur.fetchall()
        expected = (row.get("display_name", row["email"]), row.get("relationship", "技术联系人"),
                    row.get("recorded_from", "Snapshot 起点还原"))
        check(f"Agent 联系人 {row['email']}", got == [expected], row["source_id"])

    for row in state.get("catalog", []):
        got = [r for r in view.catalog(asset=row["asset"], kind=row["kind"])
               if r["asset"] == row["asset"] and r["key"] == row["key"]]
        check(f"Agent 当前档案 {row['asset']}/{row['key']}", len(got) == 1 and got[0]["value"] == row["value"]
              and got[0]["status"] == row.get("status", "inferred"), str(got))

    for row in state.get("runs") or []:
        run_id = ids.get(("run", row["id"]))
        cur.execute("SELECT status, waiting_on, next_action_at FROM runs WHERE run_id=%s",
                    (run_id,))
        got = cur.fetchone()
        check(f"任务线 {row['id']}", got is not None and got[0] == row.get(
            "status", "waiting_human"), f"{got}")

    for row in state.get("tickets") or []:
        ticket_id = ids.get(("ticket", row["id"]))
        cur.execute("SELECT approver, expires_at > now(), used_at IS NULL"
                    " FROM approvals WHERE id=%s", (ticket_id,))
        got = cur.fetchone()
        check(f"票据 {row['id']}", got is not None, "票据没落库")
        check(f"票据 {row['id']} 审批人/消费状态", got[0] == row.get("approver", "steward")
              and got[2] == (row.get("used_h") is None), str(got))
        expected_live = float(row.get("expires_h", 72)) > 0
        check(f"票据 {row['id']} 有效期", got[1] == expected_live,
              f"expires_h={row.get('expires_h', 72)} 实际未过期={got[1]}")
        if row.get("decision"):
            cur.execute("SELECT decision,chosen FROM decisions WHERE approval_id=%s", (ticket_id,))
            check(f"历史决定 {row['id']}", cur.fetchall() == [(row["decision"], row.get("chosen"))], row["decision"])
        if row.get("waits"):
            # 引用要连得通：`runs.resumable()` 是 runs JOIN decisions ON waiting_on。
            cur.execute("SELECT count(*) FROM runs r JOIN decisions d"
                        " ON d.approval_id::text = r.waiting_on WHERE r.waiting_on=%s",
                        (str(ticket_id),))
            linked = cur.fetchone()[0]
            check(f"票据 {row['id']} 与任务线连通",
                  linked == (1 if row.get("decision") else 0),
                  f"JOIN 到 {linked} 条；有决定={bool(row.get('decision'))}")
    return checks


def restore(state: dict, *, baseline=None, origin=None) -> dict:
    """写起点并自验。返回还原清单与自验结果，供观察器归档。"""
    if not isinstance(baseline, dict) or not baseline.get("origin") or baseline.get("kind") not in ("bundle", "empty"):
        raise RestoreFailed("state 只能覆盖 bundle 或显式 empty 基线；禁止隐含的纯 state 起点")
    if baseline["kind"] == "empty" and baseline.get("empty_planes") != ["session", "memory", "lake", "cron"]:
        raise RestoreFailed("empty 基线必须逐项声明 session/memory/lake/cron 为空")
    if state and (not isinstance(origin, str) or not origin.startswith(f"derived:{baseline['origin']}/")):
        raise RestoreFailed("覆盖层必须声明 origin: derived:<基线>/<改动>")
    check_state(state)
    ids: dict = {}
    restored: list[str] = []
    conn = _connect()
    anchor = _ANCHOR.set(time.time())
    try:
        with conn.cursor() as cur:
            from psycopg import sql
            tables = {TABLES[k] for k, rows in state.items() if rows and k in TABLES}
            tables.update(row["table"] for row in state.get("patches", []))
            if state.get("tickets"):
                tables.add("decisions")
            def dump():
                out = {}
                for table in sorted(tables):
                    cur.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
                    names = [c.name for c in cur.description]
                    out[table] = [_json_value(dict(zip(names, row))) for row in cur.fetchall()]
                return out
            before = dump()
            for key, writer in WRITERS:
                rows = state.get(key)
                if rows:
                    restored += writer(cur, rows, ids)
            after = dump()
            # Verify with the same database role as Hermes; privilege-dependent
            # visibility cannot be inferred from an admin SELECT.
            cur.execute("SET LOCAL ROLE agent_role")
            checks = verify(cur, state, ids)
            cur.execute("RESET ROLE")
        conn.commit()          # 自验过了才落盘
    except Exception:
        conn.rollback()        # 半份起点绝不留下
        raise
    finally:
        _ANCHOR.reset(anchor)
        conn.close()
    overrides = []
    for table in sorted(tables):
        removed = [row for row in before[table] if row not in after[table]]
        added = [row for row in after[table] if row not in before[table]]
        if removed or added:
            overrides.append({"table": table, "before": removed, "after": added})
    return {"restored": restored, "checks": checks, "applicable": True,
            "from_bundle": baseline if baseline["kind"] == "bundle" else None,
            "empty_baseline": baseline if baseline["kind"] == "empty" else None,
            "origin": origin, "overridden": overrides,
            "ids": {f"{kind}:{name}": value for (kind, name), value in ids.items()}}


def main(argv=None) -> int:
    """独立进程入口：`python services/snapshot_restore.py <state.json>`。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：snapshot_restore.py <state.json>", file=sys.stderr)
        return 2
    try:
        value = json.load(sys.stdin) if argv[0] == "-" else json.loads(pathlib.Path(argv[0]).read_text(encoding="utf-8"))
        if set(value) != {"baseline", "origin", "state"}:
            raise RestoreFailed("还原请求必须只含 baseline/origin/state")
        result = restore(value["state"], baseline=value["baseline"], origin=value["origin"])
    except Exception as exc:
        from snapshot_runner.input_audit import scrub
        print(json.dumps({"ok": False, "error": scrub(str(exc))}, ensure_ascii=False))
        return 1
    from snapshot_runner.input_audit import scrub
    print(json.dumps(scrub({"ok": True, **result}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
