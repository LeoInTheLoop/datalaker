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
    try:
        from plugins.datasteward_gate.approvals import open_store
    except ModuleNotFoundError as exc:
        # Hermes owns a regular top-level ``plugins`` package.  Its cron
        # monitor runs as a child process where that package can shadow this
        # project's namespace package; the project plugin is also on
        # PYTHONPATH as ``datasteward_gate``.
        if not (exc.name or "").startswith("plugins"):
            raise
        from datasteward_gate.approvals import open_store
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
        "note", "owner_role", "created_at", "updated_at", "resumed",
        "next_action_at")


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


def schedule(run_id: str, at: float, reason: str = "") -> dict:
    """排期：**到这个时刻之前，这条线不算可推进**。

    「已批准，但窗口在今晚 01:00」只能这样表达 —— 没有它的时候，
    批准一落库 monitor 立刻就把线拉起来（窗口外执行），
    或者每分钟去问一遍（等于没有排期）。

    不改 status：等谁、在等什么都没变，变的只是「什么时候该再看一眼」。
    """
    kw = {"next_action_at": float(at)}
    if reason:
        kw["note"] = reason[:400]
    _set(run_id, **kw)
    return {"run_id": run_id, "next_action_at": float(at), "reason": reason}


def unschedule(run_id: str) -> dict:
    """撤掉排期，这条线立刻回到「随时可推进」。"""
    _set(run_id, next_action_at=None)
    return {"run_id": run_id, "next_action_at": None}


def due_now(r: dict, now: float | None = None) -> bool:
    """这条线现在该被碰了吗。

    **没排期的一律算「到点」** —— 排期是额外的一道闸，不是推进的必要条件。
    取不到或存了脏值时同样算到点：宁可多醒一次，也不要让一条线因为一个
    坏字段永远醒不过来。
    """
    v = r.get("next_action_at")
    if v in (None, ""):
        return True
    try:
        return float(v) <= (time.time() if now is None else now)
    except (TypeError, ValueError):
        return True


def scheduled(now: float | None = None) -> list:
    """排了期、但还没到点的线。给状态查询用，不给恢复用。"""
    return [r for r in by_status("waiting_human") + by_status("running")
            if not due_now(r, now)]


def due(now: float | None = None) -> list:
    """到点了、且**不在等任何审批**的线 —— 等的就是这个时刻本身。

    等审批的那些到点后由 `resumable()` 收走（它们要重放票里的参数），
    这里只剩「时间到了就该再看一眼」的那一类，否则同一条线会被报两次。
    """
    return [r for r in by_status("waiting_human") + by_status("running")
            if r.get("next_action_at") not in (None, "")
            and due_now(r, now) and not r.get("waiting_on")]


def decision_of(approval_id: str | None) -> str | None:
    """这条线等的那份审批，人已经给决定了吗？

    返回 approve / deny / answered，没决定返回 None。
    **读的是 `decisions`，不是 `approvals`** —— 审批请求发出去了不等于有人点过。
    """
    if not approval_id:
        return None
    try:
        with _store(readonly=True) as st:
            r = _exec(st,
                      "SELECT decision FROM decisions WHERE approval_id=?"
                      " ORDER BY decided_at DESC LIMIT 1",
                      "SELECT decision FROM decisions WHERE approval_id::text=%s"
                      " ORDER BY decided_at DESC LIMIT 1",
                      (approval_id,), fetch=True)
        return r[0][0] if r else None
    except Exception:                                        # noqa: BLE001
        return None


def approver_of(approval_id: str | None) -> str:
    """这份审批发给了谁（角色名或邮箱）；查不到返回空串。"""
    return _approver_of(approval_id) if approval_id else ""


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


def set_checkpoint(run_id: str, checkpoint: dict, note: str = "") -> dict:
    """只更新这条线的 checkpoint（等窗口、分片进度这类进展）。

    **不能用 `suspend` 代替**：suspend 对「已经在等同一份审批」是幂等的，
    而「已批准、现在改成等执行窗口」正好落进那个幂等分支 —— 写不进去，
    外面还看着像写成功了。等谁没变、状态没变，变的只是进展，就用这个。
    """
    kw = {"checkpoint": json.dumps(checkpoint or {}, ensure_ascii=False)}
    if note:
        kw["note"] = note[:400]
    _set(run_id, **kw)
    return {"run_id": run_id, "checkpoint": checkpoint}


def finish(run_id, status="done", note=""):
    assert status in STATUSES
    # 排期一并清掉：收了口的线不该再被「到点」叫醒一次。
    _set(run_id, status=status, note=note, waiting_on=None, next_action_at=None)
    return {"run_id": run_id, "status": status}


def bump_resumed(run_id):
    # 拉起来就把排期清掉 —— 留着的话这条线每个整点会被当成「又到点了」
    # 再叫一次，而它已经在跑了。
    with _store() as st:
        _exec(st, "UPDATE runs SET resumed=resumed+1, status='running',"
                  " next_action_at=NULL WHERE run_id=?",
              "UPDATE runs SET resumed=resumed+1, status='running',"
              " next_action_at=NULL WHERE run_id=%s",
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
                     " JOIN decisions d ON d.approval_id::text = r.waiting_on"
                     " WHERE r.status='waiting_human' ORDER BY d.decided_at",
                     (), fetch=True)
    # **没到点的不算可推进。** 票批下来了不等于现在就能跑 ——
    # 窗口在今晚 01:00 的那条线，白天被拉起来就是窗口外执行。
    return [r for r in (_row(x) for x in rows or []) if due_now(r)]


def retryable() -> list:
    """挂起但没在等任何审批的线 —— 典型是被 WIP 限制挡回来的。

    它们与 `resumable()` 是两回事：那边等的是**人的决定**，
    这边等的是**别人的待办降下来**。混在一起会让「谁先被批准谁先被拉起」失真。

    **排了期的线不在这里**，它们等的是某个时刻，走 `due()`。
    混进来的话，「今晚 01:00 再跑」会被当成「被 WIP 挡回来了，赶紧重试」。
    """
    return [r for r in by_status("waiting_human")
            if not r["waiting_on"] and due_now(r)
            and r.get("next_action_at") in (None, "")]


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
                     " ON a.id::text = r.waiting_on WHERE r.status='waiting_human'"
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
