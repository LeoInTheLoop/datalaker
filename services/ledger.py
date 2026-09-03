"""Remediation Ledger —— 整改台账（readme 7 / 7.1）。

原表不动，只动复制过来的副本；但**每一次改动都留痕**，累积成反向给业务系统的
升级建议。数据问题和权限问题进同一张台账。

## 为什么必须闭环

> **只在 lake 里清洗，等于给源头的缺陷付永久利息。**

源系统不整改，每次同步都带来同样的脏数据，清洗规则要一直维护，
成本随数据量和源数量增长。所以台账的价值不在建议的数量，而在**被采纳的比例**。

## 一个反直觉的成功指标

**做得好的标志是清洗规则数量在下降。** 规则只增不减说明一直在治标；
每条规则的理想归宿是**退役**——因为它对应的源头缺陷被修掉了。
因此每条规则都必须标注 `ledger_ref`：不知道自己在补哪个洞的规则无法退役。

## 复发让证据自己说话

同一缺陷再次出现是推动整改最有力的证据，所以 `record` 是**幂等聚合**而非追加：
同一 (表, 字段, 问题) 命中已有条目就累加复发次数与累计行数，
而不是产生第 18 条一模一样的记录。
"""
import time
import uuid

STATUSES = ("open", "proposed", "accepted", "fixed", "rejected")
CATEGORIES = ("data", "permission")

COLS = ("rl_id", "source_table", "field", "issue_type", "category",
        "observed_pattern", "action_taken", "confirmed_by", "message_id",
        "suggestion", "owner_role", "status", "reject_reason", "recurrences",
        "rows_cleaned", "rule_revisions", "first_seen", "last_seen", "due_at")


def _store(readonly=False):
    from plugins.datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=not readonly)


def _lite(db):
    return hasattr(db, "execute") and not hasattr(db, "cursor_factory")


def _q(st, lite_sql, pg_sql, args=(), fetch=False):
    if _lite(st.db):
        cur = st.db.execute(lite_sql, args)
        r = cur.fetchall() if fetch else None
        st.db.commit()
        return r
    with st.db.cursor() as c:
        c.execute(pg_sql, args)
        return c.fetchall() if fetch else None


def _row(r):
    return dict(zip(COLS, r))


def _new_id():
    return "RL-" + uuid.uuid4().hex[:6].upper()


# ---------------------------------------------------------------- 记录
def record(source_table: str, issue_type: str, field: str | None = None,
           category: str = "data", observed_pattern: str = "",
           action_taken: str = "", rows_cleaned: int = 0,
           confirmed_by: str = "", message_id: str = "",
           owner_role: str | None = None) -> dict:
    """登记一次发现。**同一缺陷再次出现是累加，不是新增条目。**"""
    assert category in CATEGORIES, category
    now = time.time()
    with _store() as st:
        cur = _q(st,
                 "SELECT rl_id, recurrences, rows_cleaned FROM remediation_ledger"
                 " WHERE source_table=? AND COALESCE(field,'')=COALESCE(?,'')"
                 " AND issue_type=?",
                 "SELECT rl_id, recurrences, rows_cleaned FROM remediation_ledger"
                 " WHERE source_table=%s AND COALESCE(field,'')=COALESCE(%s,'')"
                 " AND issue_type=%s",
                 (source_table, field, issue_type), fetch=True)
        if cur:
            rl_id, rec, rows = cur[0]
            _q(st,
               "UPDATE remediation_ledger SET recurrences=?, rows_cleaned=?,"
               " last_seen=?, observed_pattern=COALESCE(NULLIF(?,''), observed_pattern),"
               " action_taken=COALESCE(NULLIF(?,''), action_taken)"
               " WHERE rl_id=?",
               "UPDATE remediation_ledger SET recurrences=%s, rows_cleaned=%s,"
               " last_seen=%s, observed_pattern=COALESCE(NULLIF(%s,''), observed_pattern),"
               " action_taken=COALESCE(NULLIF(%s,''), action_taken)"
               " WHERE rl_id=%s",
               (int(rec) + 1, int(rows) + int(rows_cleaned), now,
                observed_pattern, action_taken, rl_id))
            return {"rl_id": rl_id, "recurrences": int(rec) + 1, "new": False}
        rl_id = _new_id()
        _q(st,
           "INSERT INTO remediation_ledger (rl_id,source_table,field,issue_type,"
           "category,observed_pattern,action_taken,confirmed_by,message_id,"
           "owner_role,status,recurrences,rows_cleaned,rule_revisions,"
           "first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,'open',1,?,0,?,?)",
           "INSERT INTO remediation_ledger (rl_id,source_table,field,issue_type,"
           "category,observed_pattern,action_taken,confirmed_by,message_id,"
           "owner_role,status,recurrences,rows_cleaned,rule_revisions,"
           "first_seen,last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',1,%s,0,%s,%s)",
           (rl_id, source_table, field, issue_type, category, observed_pattern,
            action_taken, confirmed_by, message_id, owner_role,
            int(rows_cleaned), now, now))
    return {"rl_id": rl_id, "recurrences": 1, "new": True}


def get(rl_id: str) -> dict | None:
    with _store(readonly=True) as st:
        r = _q(st, f"SELECT {','.join(COLS)} FROM remediation_ledger WHERE rl_id=?",
               f"SELECT {','.join(COLS)} FROM remediation_ledger WHERE rl_id=%s",
               (rl_id,), fetch=True)
    return _row(r[0]) if r else None


def by_status(status: str) -> list:
    with _store(readonly=True) as st:
        r = _q(st, f"SELECT {','.join(COLS)} FROM remediation_ledger WHERE status=?"
                   " ORDER BY recurrences DESC",
               f"SELECT {','.join(COLS)} FROM remediation_ledger WHERE status=%s"
               " ORDER BY recurrences DESC", (status,), fetch=True)
    return [_row(x) for x in r or []]


def all_items() -> list:
    with _store(readonly=True) as st:
        r = _q(st, f"SELECT {','.join(COLS)} FROM remediation_ledger"
                   " ORDER BY recurrences DESC",
               f"SELECT {','.join(COLS)} FROM remediation_ledger"
               " ORDER BY recurrences DESC", (), fetch=True)
    return [_row(x) for x in r or []]


# ---------------------------------------------------------------- 状态机
def _set(rl_id, **kw):
    keys = list(kw)
    with _store() as st:
        _q(st, f"UPDATE remediation_ledger SET {', '.join(k + '=?' for k in keys)}"
               " WHERE rl_id=?",
           f"UPDATE remediation_ledger SET {', '.join(k + '=%s' for k in keys)}"
           " WHERE rl_id=%s", tuple(kw[k] for k in keys) + (rl_id,))


def propose(rl_id: str, suggestion: str, owner_role: str, due_days: int = 14) -> dict:
    """发出整改建议。**发出去就不管等于没发**，因此必须有主人和期限（7.1）。"""
    if not suggestion.strip():
        raise ValueError("建议不能为空")
    _set(rl_id, suggestion=suggestion, owner_role=owner_role, status="proposed",
         due_at=time.time() + due_days * 86400)
    return get(rl_id)


def accept(rl_id, by): _set(rl_id, status="accepted", confirmed_by=by); return get(rl_id)


def mark_fixed(rl_id, by):
    """源头修好了。**这会让挂在它上面的清洗规则具备退役条件。**"""
    _set(rl_id, status="fixed", confirmed_by=by)
    return get(rl_id)


def reject(rl_id: str, by: str, reason: str) -> dict:
    """「已拒绝」是合法结局 —— 业务上可能确实允许该字段脏。

    但**必须留痕且带理由**，此后不再重复打扰，等同于清洗侧的 deny list。
    """
    if not reason.strip():
        raise ValueError("拒绝必须给理由 —— 否则下次还会再问一遍")
    _set(rl_id, status="rejected", confirmed_by=by, reject_reason=reason)
    return get(rl_id)


def overdue(now: float | None = None) -> list:
    """超期未处理的建议。走与审批同一套超时时钟（7.1）。"""
    t = now or time.time()
    return [x for x in all_items()
            if x["status"] in ("proposed", "accepted") and x["due_at"]
            and x["due_at"] < t]


# ---------------------------------------------------------------- 清洗规则
def add_rule(name: str, ledger_ref: str, fixes: str, retire_when: str = "") -> dict:
    """登记一条清洗规则。**没有 ledger_ref 不许登记** —— 见模块文档。"""
    if not get(ledger_ref):
        raise ValueError(f"{ledger_ref} 不在台账里：不知道自己在补哪个洞的规则无法退役")
    with _store() as st:
        _q(st, "INSERT INTO cleaning_rules (name,ledger_ref,fixes,retire_when,"
               "active,revisions,created_at) VALUES (?,?,?,?,1,0,?)"
               " ON CONFLICT(name) DO UPDATE SET revisions=cleaning_rules.revisions+1",
           "INSERT INTO cleaning_rules (name,ledger_ref,fixes,retire_when,"
           "active,revisions,created_at) VALUES (%s,%s,%s,%s,1,0,%s)"
           " ON CONFLICT (name) DO UPDATE SET revisions=cleaning_rules.revisions+1",
           (name, ledger_ref, fixes, retire_when, time.time()))
    return {"name": name, "ledger_ref": ledger_ref, "active": True}


def rules(active_only=True) -> list:
    with _store(readonly=True) as st:
        r = _q(st, "SELECT name,ledger_ref,fixes,retire_when,active,revisions,"
                   "created_at,retired_at FROM cleaning_rules"
                   + (" WHERE active=1" if active_only else ""),
               "SELECT name,ledger_ref,fixes,retire_when,active,revisions,"
               "created_at,retired_at FROM cleaning_rules"
               + (" WHERE active=1" if active_only else ""), (), fetch=True)
    k = ("name", "ledger_ref", "fixes", "retire_when", "active", "revisions",
         "created_at", "retired_at")
    return [dict(zip(k, x)) for x in r or []]


def retirable() -> list:
    """源头已修、因而可以退役的规则。**这是成功指标的分子。**"""
    fixed = {x["rl_id"] for x in by_status("fixed")}
    return [r for r in rules() if r["ledger_ref"] in fixed]


def retire(name: str) -> dict:
    with _store() as st:
        _q(st, "UPDATE cleaning_rules SET active=0, retired_at=? WHERE name=?",
           "UPDATE cleaning_rules SET active=0, retired_at=%s WHERE name=%s",
           (time.time(), name))
    return {"name": name, "active": False}


# ---------------------------------------------------------------- 指标
def metrics() -> dict:
    """治理闭环指标（7.1）。**活跃规则数应当随时间下降。**"""
    items = all_items()
    prop = [x for x in items if x["status"] != "open"]
    accepted = [x for x in items if x["status"] in ("accepted", "fixed")]
    act = rules(active_only=True)
    all_r = rules(active_only=False)
    return {
        "active_rules": len(act),
        "retired_rules": len(all_r) - len(act),
        "retirable_now": len(retirable()),
        "proposals": len(prop),
        "adoption_rate": round(len(accepted) / len(prop), 3) if prop else None,
        "total_recurrences": sum(x["recurrences"] for x in items),
        "rows_cleaned": sum(x["rows_cleaned"] for x in items),
        "open": len([x for x in items if x["status"] == "open"]),
        "rejected": len([x for x in items if x["status"] == "rejected"]),
        "overdue": len(overdue()),
        "note": "活跃规则数下降 = 做得好；只增不减 = 一直在治标（7.1）",
    }


def evidence(rl_id: str) -> str:
    """给人看的那段话 —— 把技术债务量化成具体行数和次数（7.1）。"""
    x = get(rl_id)
    if not x:
        return f"{rl_id} 不存在"
    import datetime
    d = datetime.date.fromtimestamp(x["first_seen"])
    return (f"{x['source_table']}{'.' + x['field'] if x['field'] else ''}  "
            f"{x['issue_type']}\n"
            f"  首次发现   {d}\n"
            f"  复发       {x['recurrences']} 次\n"
            f"  累计清洗   {x['rows_cleaned']:,} 行\n"
            f"  规则维护   {x['rule_revisions']} 次\n"
            f"  建议       {x['suggestion'] or '（尚未提出）'}")
