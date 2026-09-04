"""任务注册表 —— 让任务活得比进程久（readme 2「Harness 能力」/ 4.1）。

**Agent 不是一次调用，它会等人。** 一条线的真实形状是：

    发现数据 → 找 Owner → 发邮件 → 等 8 小时 → Owner 回复
              → 请求审批 → 等 1 天 → 恢复任务 → 执行 SQL → 数据校验 → 再找 Steward

而且同时并行着好几条，挂在不同的人身上。因此有两条硬约束：

1. **状态不能只活在进程里。** 进程会死、机器会重启、人会隔天才回。
   状态落 `runs` 表，进程只是执行器。
2. **各条线彼此独立。** 一条卡在王姐那儿，不该拖住卡在李工那儿的另一条。
   谁的审批先落，谁先被拉起 —— 顺序由人的响应决定，不由代码里的顺序决定。

它与 LangGraph 的 checkpointer 是两层：
checkpointer 管**单次执行内**的节点续跑，`runs` 管**跨天跨进程**的挂起恢复。
（`pipelines/ingest_table.py` 顶部说明了为什么不用 `interrupt`。）

    创建 ──► running ──gate PENDING──► waiting_human ──审批落库──► running ──► done
                                            │
                                       超时未响应 ──► abandoned
"""
import json
import time
import uuid

try:                                     # 旁路观测：没装 SDK / 没配端点都当没有
    import tracing
except Exception:                                            # noqa: BLE001
    tracing = None

STATUSES = ("running", "waiting_human", "done", "abandoned", "failed")

# kind → 可调用的执行器。**新增一种任务 = 注册一行**，恢复逻辑不用改。
RUNNERS: dict = {}


def register(kind: str, fn):
    RUNNERS[kind] = fn
    return fn


def _store(readonly=False):
    from plugins.datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=not readonly)


def _sqlite(db):
    return hasattr(db, "execute") and not hasattr(db, "cursor_factory")


def _exec(st, sql_lite, sql_pg, args=(), fetch=False):
    if _sqlite(st.db):
        cur = st.db.execute(sql_lite, args)
        rows = cur.fetchall() if fetch else None
        st.db.commit()
        return rows
    with st.db.cursor() as c:
        c.execute(sql_pg, args)
        return c.fetchall() if fetch else None


COLS = ("run_id", "kind", "params", "status", "waiting_on", "checkpoint",
        "note", "owner_role", "created_at", "updated_at", "resumed")


def _row(r):
    d = dict(zip(COLS, r))
    for k in ("params", "checkpoint"):
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except Exception:                                    # noqa: BLE001
            pass
    return d


# ---------------------------------------------------------------- 写
def create(kind: str, params: dict, run_id: str | None = None,
           note: str = "", owner_role: str | None = None) -> str:
    rid = run_id or f"{kind}-{uuid.uuid4().hex[:8]}"
    now = time.time()
    with _store() as st:
        _exec(st,
              "INSERT INTO runs (run_id,kind,params,status,waiting_on,checkpoint,"
              "note,owner_role,created_at,updated_at,resumed)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,0)"
              " ON CONFLICT(run_id) DO NOTHING",
              "INSERT INTO runs (run_id,kind,params,status,waiting_on,checkpoint,"
              "note,owner_role,created_at,updated_at,resumed)"
              " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)"
              " ON CONFLICT (run_id) DO NOTHING",
              (rid, kind, json.dumps(params, ensure_ascii=False), "running",
               None, None, note, owner_role, now, now))
    return rid


def _set(run_id, **kw):
    kw["updated_at"] = time.time()
    keys = list(kw)
    with _store() as st:
        _exec(st,
              f"UPDATE runs SET {', '.join(k + '=?' for k in keys)} WHERE run_id=?",
              f"UPDATE runs SET {', '.join(k + '=%s' for k in keys)} WHERE run_id=%s",
              tuple(kw[k] for k in keys) + (run_id,))


def _approver_of(approval_id):
    """挂起 span 的「等谁」。`runs.owner_role` 建线时常为空，
    真正的收件人写在 `approvals.approver` 上，只能去那儿取。"""
    try:
        with _store(readonly=True) as st:
            r = _exec(st, "SELECT approver FROM approvals WHERE id=?",
                      "SELECT approver FROM approvals WHERE id=%s",
                      (approval_id,), fetch=True)
        return r[0][0] if r else ""
    except Exception:                                        # noqa: BLE001
        return ""


def suspend(run_id: str, waiting_on: str | None, checkpoint: dict | None = None,
            note: str = "") -> dict:
    """挂起等人。**这不是失败**，是这类任务的正常状态。

    **幂等**：已经在等同一份审批就直接返回。M5 之后同一次挂起会被两处
    看到 —— 门禁（它最先知道）和 Pipeline 包装（旧路径）—— 不幂等的话
    `suspend_begin` 会开出两条 WAITING_FOR_HUMAN span，等待时长凭空翻倍。
    """
    cur = get(run_id)
    if cur and cur["status"] == "waiting_human" and cur["waiting_on"] == waiting_on:
        return {"run_id": run_id, "status": "waiting_human",
                "waiting_on": waiting_on, "already": True}
    _set(run_id, status="waiting_human", waiting_on=waiting_on,
         checkpoint=json.dumps(checkpoint or {}, ensure_ascii=False), note=note)
    # 只有真在等人才算 WAITING_FOR_HUMAN；等 WIP 容量是排队，不是等谁拍板
    if waiting_on and tracing and tracing.enabled():
        tracing.suspend_begin(run_id, waiting_on, _approver_of(waiting_on), note)
    return {"run_id": run_id, "status": "waiting_human", "waiting_on": waiting_on}


def finish(run_id, status="done", note=""):
    assert status in STATUSES
    _set(run_id, status=status, note=note, waiting_on=None)
    return {"run_id": run_id, "status": status}


def bump_resumed(run_id):
    with _store() as st:
        _exec(st, "UPDATE runs SET resumed=resumed+1, status='running' WHERE run_id=?",
              "UPDATE runs SET resumed=resumed+1, status='running' WHERE run_id=%s",
              (run_id,))


# ---------------------------------------------------------------- 读
def get(run_id: str) -> dict | None:
    with _store(readonly=True) as st:
        r = _exec(st, f"SELECT {','.join(COLS)} FROM runs WHERE run_id=?",
                  f"SELECT {','.join(COLS)} FROM runs WHERE run_id=%s",
                  (run_id,), fetch=True)
    return _row(r[0]) if r else None


def by_status(status: str) -> list:
    with _store(readonly=True) as st:
        rows = _exec(st, f"SELECT {','.join(COLS)} FROM runs WHERE status=?"
                         " ORDER BY updated_at",
                     f"SELECT {','.join(COLS)} FROM runs WHERE status=%s"
                     " ORDER BY updated_at", (status,), fetch=True)
    return [_row(r) for r in rows or []]


def resumable() -> list:
    """哪些线现在可以往下走了 —— 它们等的那份审批已经有决定了。

    **顺序由人的响应决定**：谁先批，谁先被拉起。
    这也是为什么恢复要查库而不是靠内存队列 —— 内存里没有「谁先回信」这个信息。
    """
    with _store(readonly=True) as st:
        rows = _exec(st,
                     f"SELECT {','.join('r.' + c for c in COLS)} FROM runs r"
                     " JOIN decisions d ON d.approval_id = r.waiting_on"
                     " WHERE r.status='waiting_human' ORDER BY d.decided_at",
                     f"SELECT {','.join('r.' + c for c in COLS)} FROM runs r"
                     " JOIN decisions d ON d.approval_id = r.waiting_on"
                     " WHERE r.status='waiting_human' ORDER BY d.decided_at",
                     (), fetch=True)
    return [_row(r) for r in rows or []]


def retryable() -> list:
    """挂起但没在等任何审批的线 —— 典型是被 WIP 限制挡回来的。

    它们与 `resumable()` 是两回事：那边等的是**人的决定**，
    这边等的是**别人的待办降下来**。混在一起会让「谁先被批准谁先被拉起」失真。
    """
    return [r for r in by_status("waiting_human") if not r["waiting_on"]]


def stuck(min_age_h: float = 0.0) -> list:
    """挂起中且还没被批的线 —— 周报里「卡在谁那里」就是这张表。"""
    cutoff = time.time() - min_age_h * 3600
    return [r for r in by_status("waiting_human") if r["updated_at"] <= cutoff]


def summary() -> dict:
    out = {s: len(by_status(s)) for s in STATUSES}
    out["total"] = sum(out.values())
    return out


# ---------------------------------------------------------------- 恢复
def reconcile_abandoned() -> list:
    """等的那份审批已被放弃 → 这条线也收口。

    没有这一步，升级链跑到 ABANDONED 之后**任务本身还在 waiting_human 里等**，
    等一个永远不会到来的决定。放弃不是删除：event log 与台账都在，
    人想起来随时可以手动重开（readme 5.4）。
    """
    out = []
    with _store(readonly=True) as st:
        rows = _exec(st,
                     "SELECT r.run_id FROM runs r JOIN approvals a"
                     " ON a.id = r.waiting_on WHERE r.status='waiting_human'"
                     " AND a.abandoned_at IS NOT NULL",
                     "SELECT r.run_id FROM runs r JOIN approvals a"
                     " ON a.id = r.waiting_on WHERE r.status='waiting_human'"
                     " AND a.abandoned_at IS NOT NULL", (), fetch=True)
    for (rid,) in rows or []:
        finish(rid, "abandoned", "等的审批已超时放弃，转为已知阻塞项")
        out.append(rid)
    return out


def resume_one(run_id: str) -> dict:
    """把一条线往下推一步。执行器自己再挂起或收尾。

    **一次只推一条，不合批。** 合批会让一条线的失败牵连另一条，
    而它们本来就属于不同的人、不同的资产。
    """
    r = get(run_id)
    if not r:
        return {"run_id": run_id, "status": "missing"}
    fn = RUNNERS.get(r["kind"])
    if not fn:
        return {"run_id": run_id, "status": "no_runner", "kind": r["kind"]}
    # 等待到此为止。起点取 DB 里记的挂起时刻——它活得比进程久，
    # 也是时间引擎回拨过的那一个，所以跨度是真等了多久，不是本进程等了多久。
    if r["waiting_on"] and tracing and tracing.enabled():
        tracing.suspend_end(run_id, since=r["updated_at"],
                            approval_id=r["waiting_on"],
                            waiting_for=_approver_of(r["waiting_on"]),
                            note=r["note"] or "")
    bump_resumed(run_id)
    try:
        out = fn(run_id=run_id, params=r["params"], checkpoint=r["checkpoint"])
    except Exception as e:                                   # noqa: BLE001
        finish(run_id, "failed", f"{type(e).__name__}: {e}")
        return {"run_id": run_id, "status": "failed", "error": str(e)[:200]}
    return {"run_id": run_id, "status": get(run_id)["status"], "result": out}


def resume_all(limit: int = 50) -> list:
    """把所有可恢复的线各推一步。互不影响 —— 一条崩了另一条照走。"""
    return [resume_one(r["run_id"]) for r in resumable()[:limit]]
