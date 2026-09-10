"""Data Steward Claw —— Hermes 治理 Plugin。

挂载点（readme 4.0 Hook 映射）：
  pre_tool_call        Directive，可 block —— 分级 / 票据 / 指纹 / deny list / 参数改写
  post_tool_call       Observer          —— event log、负载记账
  pre_approval_request Observer          —— 发审批邮件

Hermes 契约要点：
  - pre_tool_call 在 handle_function_call() 内、工具 handler 执行之前触发
  - 返回 {"action":"block","message":...} 短路执行，message 作为 error 回给模型
  - 返回 {"action":"modify","args":{...}} 浅合并进工具参数
  - 回调超时或异常 → Hermes fail closed（阻断而非放行）
"""
import json
import time
import threading
import os

from .approvals import Store, open_store, rows_of
from .policy import (HERMES_DANGEROUS, Level, arg_denied, effective,
                     is_declared, lookup, manual_approver)

DB_PATH = os.environ.get("DATASTEWARD_DB", os.path.expanduser("~/.datalaker/approvals.db"))
# 每个线程一个句柄：Hermes 在线程池里跑工具，SQLite 连接不许跨线程用
_local = threading.local()


def store():
    """Agent 侧存储句柄。**每个线程一个。**

    DATASTEWARD_DSN 存在 -> Postgres（生产 / 容器内，列级 GRANT 在此生效）
    否则                  -> SQLite（无 docker 时的本地快测）

    早先缓存成模块级单例，在单线程驱动下没问题。搬进 Hermes 之后立刻炸：
    它在线程池里跑工具 handler，SQLite 连接不许跨线程用 ——
    报「object was created in thread id X and this is thread id Y」，
    而错误被 handler 吞成一句「失败」，表面上像是工具本身有问题。

    `_notify_async` 里其实早就写过这条注释（「后台线程必须建自己的连接」），
    但只修了那一处。这次改成线程本地，所有路径一起管。
    """
    h = getattr(_local, "store", None)
    if h is None:
        h = open_store(readonly=True)            # Agent 侧只读（readme 9.4 机制三）
        _local.store = h
    return h


# --------------------------------------------------------------------------
# pre_tool_call —— 唯一可否决的挂载点
# --------------------------------------------------------------------------
def gate(tool_name: str, args: dict, task_id: str = "", **kwargs):
    """pre_tool_call 入口。**任何异常都必须转成 block。**

    Hermes 在 agent_runtime_helpers.py 的 dispatch 层写着：

        except Exception:
            block_message = None      # ← 异常冒到这里，工具会被放行

    单个 callback 的异常虽然由 invoke_hook 吞掉，但我们不能把安全性
    寄托在上游的异常处理细节上。数据库连不上、配置缺失、代码 bug——
    任何情况下都宁可挡住，不可放行。
    """
    try:
        r = _gate(tool_name, args, task_id, **kwargs)
    except Exception as e:
        r = {"action": "block",
             "message": f"[GATE_ERROR] 治理组件异常，已按 fail-closed 拒绝执行 "
                        f"{tool_name}：{type(e).__name__}: {e}"}
    _audit_block(tool_name, task_id, r)
    return r


def _audit_block(tool_name, task_id, r):
    """**拦下来这件事本身要留痕。**

    被拦时工具 handler 根本不跑，于是 `post_tool_call` 的 `TOOL_*` 事件
    也不会有 —— 事件日志里只看得见「成功执行的」，看不见「被挡住的」。
    运维面板因此永远显示一切正常，轨迹里也复盘不出是哪一步被卡住。

    观察者：写不进去不改变判断（门禁比记录重要得多）。
    """
    if not (isinstance(r, dict) and r.get("action") == "block"):
        return
    msg = str(r.get("message") or "")
    code = msg[1:msg.index("]")] if msg.startswith("[") and "]" in msg else "BLOCK"
    try:
        store().append_event(task_id or "gate", f"BLOCKED_{code}",
                             json.dumps({"tool": tool_name, "msg": msg[:200]},
                                        ensure_ascii=False))
    except Exception:                                        # noqa: BLE001
        pass


_budget_cache = {"ts": 0.0, "over": None}


def _budget_exceeded():
    """预算硬停（readme 5.6 / 20.2）。

    超限是**挂起**，不是告警——告警没人看，挂起才停得住。
    每次工具调用都查库太贵，缓存 60 秒；预算是天级的，60 秒精度足够。
    """
    import time as _t
    if _t.time() - _budget_cache["ts"] < 60:
        return _budget_cache["over"]

    over = None
    try:
        import os as _o
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "services"))
        import connector
        u = connector.today_usage()
        tl = int(_o.environ.get("DAILY_TOKEN_LIMIT", "0") or 0)
        cl = float(_o.environ.get("DAILY_COST_LIMIT_USD", "0") or 0)
        if tl and u["tokens"] >= tl:
            over = f"今日 token {u['tokens']:,} 已达上限 {tl:,}"
        elif cl and u["cost_usd"] >= cl:
            over = f"今日花费 ${u['cost_usd']:.2f} 已达上限 ${cl}"
    except Exception:
        over = None                       # 查不到预算不阻断正常工作
    _budget_cache.update(ts=_t.time(), over=over)
    return over


def _gate(tool_name: str, args: dict, task_id: str = "", **kwargs):
    # `effective` = 策略表 + 手动模式提级（MANUAL_MODE）。
    # 提级发生在这一行，不是散在下面每个分支里——散着写必然漏一个，
    # 而漏掉的那个就是手动模式下唯一一条没人看就执行了的路。
    level, approver_role = effective(tool_name)
    # **两个级别不是一回事，别混用。**
    #   level    = 提级后的，决定「要不要票」
    #   declared = 策略表里写的，决定「这是哪一类动作」
    # 预算与静默期问的是后者。用提级后的那个去判静默期会死锁：
    # `propose_stage_decision` 本是 L1、正是打破静默的**唯一**那条路，
    # 手动模式把它抬成 L2 之后，静默期连它一起挡住，于是这一轮永远
    # 出不去 —— 而表面上看只是「Agent 什么都不做了」。
    declared = lookup(tool_name)[0]

    # 预算兜底：只挡消耗型动作，不挡纯读元数据（否则连状态都查不了）
    if declared >= Level.L1:
        over = _budget_exceeded()
        if over:
            return {"action": "block",
                    "message": f"[BUDGET] {over}。今日不再执行 {tool_name}，"
                               f"请明日继续或调整 .env 中的上限。"}
    st = store()
    h = st.action_hash(tool_name, args)

    # 未声明工具：直接拒绝，不发审批
    if not is_declared(tool_name):
        why = ("该工具可执行任意命令，会绕过 Connector 的查询护栏与审批门禁"
               if tool_name in HERMES_DANGEROUS else "该工具未在治理策略中声明")
        return {"action": "block",
                "message": f"[NOT_DECLARED] {tool_name} 不可用：{why}。"
                           f"如确需使用，请在 plugins/datasteward_gate/policy.py "
                           f"中显式声明其自主性级别。"}

    # L4：永不自动执行
    if level >= Level.L4:
        return {"action": "block",
                "message": f"[L4] {tool_name} 永不自动执行，需线下双人批准后由人工操作。"}

    # 参数级禁令：工具名合法但这次的参数把它变成了另一件事
    # （`grant_read(principal="claw")` = 自我提权）。按 L4 处理：拒绝，不发审批。
    why = arg_denied(tool_name, args)
    if why:
        return {"action": "block",
                "message": f"[L4] {tool_name} 这次调用被拒绝：{why}。"}

    # 没人提过的源，碰都不该碰（readme 8 / evals v2 发现机制）
    why = _source_not_granted(st, args, tool_name)
    if why:
        return {"action": "block", "message": why}
    # The model may use a case title (for example `Northwind`) instead of the
    # stable source id (`northwind`).  Hash the effective, canonical arguments
    # after the allow-list check so approval replay remains idempotent.
    h = st.action_hash(tool_name, args)

    # 轮级的门：没人拍板开清洗轮，就不许洗（acme_full_v2.md §3）
    why = _silver_round_closed(st, tool_name)
    if why:
        return {"action": "block", "message": why}

    # 出了阶段报告就该安静下来 —— 停得下来也是能力
    why = _quiet_period(st, tool_name, declared)
    if why:
        return {"action": "block", "message": why}

    # sql_query 是 L1 工具，但风险不只由工具名决定：同一个工具里，
    # SELECT 10 行和 JOIN 大表不是同一类动作。因此 SQL 走内容级动态准入。
    if tool_name == "sql_query":
        return _sql_guard(args, task_id=task_id, st=st)

    # 已经做完的事不要再发一次审批 —— 人白点链接（见下）。
    why = _already_done(st, tool_name, args)
    if why:
        return {"action": "block", "message": why}

    # 先找已批准但尚未消费的票，再检查 dsn。恢复作业会按安全约定隐藏
    # dsn；如果先做「无 dsn」检查，就会把真正已经批准的接入线误判成坏账号
    # 重试，永远走不到 `_replay_approved_args`。
    held = None
    if level >= Level.L2:
        held = st.find_valid(h, task_id) or _ticket_of_waiting_run(
            st, tool_name, args, task_id=task_id)

    # 没有连接串就不要开审批票 —— 人收到也没法处理（见下）。
    why = _connect_without_dsn(st, tool_name, args)
    if why and not held:
        return {"action": "block", "message": why}

    # 用连接回答问题：**先查这条连接人确认过没有**，再走 SQL 那条准入。
    if tool_name == "answer_with_link":
        why = _link_not_confirmed(st, args)
        if why:
            return {"action": "block", "message": why}
        # plane 由门禁钉死成 lake，不由模型给 —— 否则「拿连接回答问题」
        # 就成了绕过 `plane=source` 那些护栏的旁路（铁律 3）。
        guard = _sql_guard({**args, "plane": "lake"}, task_id=task_id, st=st)
        if guard is not None:
            return guard
        return {"action": "modify", "args": {**args, "plane": "lake"}}

    # deny list：拒绝过的动作不再重复发起审批
    if st.is_denied(h):
        # 被拒不是失败，是**已知阻塞项**：不会就同一动作再打扰任何人。
        try:
            _track_close(task_id, tool_name, "abandoned", "审批被拒绝", args)
        except Exception:                                    # noqa: BLE001
            pass
        return {"action": "block",
                "message": f"[DENIED] {tool_name} 已被拒绝，不会重复发起审批。"
                           f"请改变方案（将产生新的动作指纹）或触发升级。"}

    # 角色补域：owner -> owner:fin（见 resolve_approver_role）
    if approver_role:
        approver_role = resolve_approver_role(st, approver_role, args)

    # **先看有没有票。** WIP 限制的是「新发起的请求会不会淹没人」——
    # 已经批准过的动作不产生新打扰，不该再被它挡。
    #
    # 顺序反了会死锁：待办堆到上限时，恢复也被 WIP 拒，而堆着的那些
    # 待办**正是这些线自己**，谁也推不动。实测撞上：6 张票已批准未用，
    # 全被「steward 当前已有 3 件待办」挡在门外。
    #
    # 两条路找票，**指纹之外还要认线**：指纹解决同义词漂移，
    # 认线解决粒度漂移（票批的是表级口径、模型回来写列级）。
    # 见 `_ticket_of_waiting_run`。
    # WIP 限制（readme 10.7）：不要淹没任何人
    if level >= Level.L2 and not held:
        over = _wip_exceeded(st, approver_role or "owner")
        if over:
            # 被 WIP 挡回**不是失败**，是「别人待办太多，等等再说」。
            # 也要记 —— 否则这条线在登记表里看起来根本没发生过。
            try:
                _track_suspend(task_id, None, tool_name, args, over)
            except Exception:                                # noqa: BLE001
                pass
            return {"action": "block", "message": over}

    # L2 / L3：需要有效票据
    if level >= Level.L2:
        tok = held
        if not tok:
            had_decision = False
            try:
                rows = rows_of(
                    st, "SELECT a.id FROM approvals a JOIN decisions d "
                    "ON d.approval_id = a.id WHERE a.action_hash = {0} "
                    "AND d.decision IS NOT NULL LIMIT 1", (h,))
                had_decision = bool(rows)
            except Exception:                                # noqa: BLE001
                pass
            payload = json.dumps(args, ensure_ascii=False)
            aid, created = st.request(task_id, h, tool_name, payload,
                                      approver_role or "owner")
            if not created and _args_changed(st, aid, args) \
                    and st.amend_pending(aid, payload):
                # 同一个动作、参数变了、而且**还没人做过决定** ——
                # 把票改成新参数并重新发信。不改的话，人批的是旧参数那份，
                # 而回填会拿它把模型这次给的新参数换掉，静默地回到老路上。
                created = True
                try:
                    st.append_event(
                        task_id or "gate", "AMEND_PENDING",
                        json.dumps({"approval_id": aid, "tool": tool_name},
                                   ensure_ascii=False))
                except Exception:                            # noqa: BLE001
                    pass
            elif created and had_decision:
                try:
                    st.append_event(
                        task_id or "gate", "NEW_TICKET_AFTER_DECISION",
                        json.dumps({"approval_id": aid, "tool": tool_name},
                                   ensure_ascii=False))
                except Exception:                            # noqa: BLE001
                    pass
            if created:
                _notify_async(aid, tool_name, args, approver_role or "owner")
            try:
                _track_suspend(task_id, aid, tool_name, args,
                               f"等 {approver_role or 'owner'} 批 {tool_name}")
            except Exception:                                # noqa: BLE001
                pass                     # 记账失败不该改变门禁的判断
            return {"action": "block",
                    "message": f"[PENDING_APPROVAL] 已就 {tool_name} 向 {approver_role} 发起审批"
                               f"（id={str(aid)[:8]}）。审批通过后本任务会被重新唤醒，"
                               f"当前不要重试，请继续处理其他不受阻塞的任务线。"}
        st.consume(tok[0])
        # **恢复时把人批准的那份参数回填。**
        #
        # 恢复发生在新会话：模型只知道「推进 acme.fin_monthly.region 那条口径」，
        # 不会把当初那一整句 value 一字不差再写一遍。指纹只认身份字段
        # （IDENTITY_KEYS），所以票据对得上；但真正要执行的参数得从
        # **人批准的那份审批**里取回来，而不是用模型这次现编的。
        #
        # 顺带把安全性提了一档：执行的永远是人看过的那一份参数，
        # 模型在恢复这一步改不了它。
        replay = _replay_approved_args(st, tool_name, tok[0], args)
        if replay is not None:
            return {"action": "modify", "args": replay}

    return None      # 放行


def _replay_approved_args(st, tool_name, approval_id, args):
    """取回这份审批当初记下的参数。只对声明了身份字段的工具生效。

    返回 None = 不回填（模型给的参数就是全部，或者取不到）。
    """
    from .canonical import RULES
    from .policy import IDENTITY_KEYS
    if tool_name not in IDENTITY_KEYS and tool_name not in RULES:
        return None
    try:
        rows = rows_of(st, "SELECT args_json FROM approvals WHERE id = {0}",
                       (approval_id,))
        if not rows:
            return None
        approved = (json.loads(rows[0][0]) if isinstance(rows[0][0], str)
                    else rows[0][0])
    except Exception:                                        # noqa: BLE001
        return None
    if not isinstance(approved, dict) or approved == dict(args or {}):
        return None
    # **声明了归一规则的工具：整份照抄批准的那一份。**
    #
    # 原来这里保留模型这次给的身份字段，理由是「它们本来就相同，
    # 指纹已经保证了」。归一之后这句话不再成立 —— `enum_rule` 与
    # `normalize_rule` 现在算同一个指纹，但它们**不是同一个字符串**；
    # 保留模型那份就等于人批了 A、系统记下 B。
    if tool_name in RULES:
        return approved
    # 其余工具行为不变：身份字段以模型这次给的为准，其余用批准的那份。
    return {**approved, **{k: v for k, v in (args or {}).items()
                           if k in IDENTITY_KEYS[tool_name]}}


def _ticket_of_waiting_run(st, tool_name: str, args: dict, task_id: str = ""):
    """这条线在等的那张票批下来没有 —— **按线找，不按指纹找。**

    为什么需要它：指纹能把同义词收敛（`enum_rule` / `normalize_rule`），
    但收敛不了**粒度**。live 实测撞到的就是这个：票批的是
    `asset="acme.fin_invoice"`，模型回来写 `asset="acme.fin_invoice.status"`
    —— 对象确实更细了一层，指纹当然不同，于是人批过的票白批，又开一张新的。

    票据本来就绑在线上（`runs.waiting_on`），那才是权威的对应关系；
    指纹是给去重和索引用的。所以恢复时先问「这条线在等的票批了吗」。

    **只放过粒度这一处差异**：工具、canonical 资产、canonical 口径类别
    三样都要一样，**只允许 target（列）不同**。

    松一点点都不行 —— 第一版写成「同一张表就绑」，结果同一张表上一条
    **全新**的口径（`key="brand_new"`）会把已批的票顺走：人批的动作照常
    执行（参数被回填），但模型真正想做的那件事被静默吞掉，而且它绕过了
    WIP 限制。`tests/test_gate.py` 第 28 组当场把这个抓出来了。
    """
    import time as _t

    from .approvals import rows_of
    from .canonical import RULES, normalize_approval_args
    from .policy import IDENTITY_KEYS
    if tool_name not in RULES and tool_name not in IDENTITY_KEYS:
        return None                      # 没声明归一规则的工具行为不变

    def _sig(a):
        """身份去掉 target —— 剩下的必须逐字相同。"""
        if tool_name in RULES:
            i = normalize_approval_args(tool_name, a)["identity"] or {}
            return (i.get("asset"), i.get("key"))
        return tuple(str((a or {}).get(k) or "").strip()
                     for k in IDENTITY_KEYS[tool_name])

    want = _sig(args)
    if not all(want):
        return None
    try:
        runs = _runs()
        for r in runs.by_status("waiting_human"):
            if r["kind"] != tool_name or not r.get("waiting_on"):
                continue
            if not isinstance(r.get("params"), dict):
                continue
            if _sig(r["params"]) != want:
                continue
            row = rows_of(st,
                          "SELECT a.id, a.expires_at FROM approvals a"
                          " JOIN decisions d ON d.approval_id = a.id"
                          " WHERE a.id = {0} AND d.decision = 'approve'"
                          " AND a.used_at IS NULL LIMIT 1",
                          (r["waiting_on"],))
            if row and _epoch(row[0][1]) > _t.time():
                try:
                    identity = (want[0] if len(want) == 1
                                else {"asset": want[0], "key": want[1]})
                    st.append_event(r["run_id"], "TICKET_BOUND_BY_RUN",
                                    json.dumps({"tool": tool_name,
                                                "identity": identity,
                                                "approval": str(row[0][0])[:8]},
                                               ensure_ascii=False))
                except Exception:                            # noqa: BLE001
                    pass
                return (row[0][0],)
    except Exception:                                        # noqa: BLE001
        return None                      # 找不到就走原路：发一张新票（吵，不松）
    return None


# 把源**加进**清单的那个动作。它不能被清单挡住 —— 否则第一个源之后
# 再也没有第二个源进得来（死锁）。这不是后门：它是 L3，要人批，
# 而且它的参数就是人在邮件里给的那串连接信息。
SOURCE_ADMITTING_TOOLS = {"connect_source", "connect_saas_control_plane"}


def _source_not_granted(st, args: dict, tool_name: str = ""):
    """授权源清单：**源是人给的，不是 Agent 自己找的。**

    未经授权的扫描本身就是违规 —— 不是勤快。清单写在人那一侧
    （`source_grants`，Agent 只有 SELECT），门禁只读它，
    限制仍然只在 hook 里（铁律 1）。

    **清单为空 = 未启用。** 与 Hermes 自己的 `_allowlist_in_effect()`
    同一条判据：没有配清单时，允许开发形态用引导源；一旦有人往里写过
    一行，它就是完整的白名单，其余一律拒。这条判据必须写在这里而不是
    环境变量里 —— 一个 `if os.environ` 就是门禁上的一个开关。

    尝试本身要留痕：碰了没人提过的库，即使被挡住，也是一次判断失误，
    判分要数得到（`evals/score_v2.trap_touches`）。
    """
    # **加入白名单的动作不能被白名单挡住。** 实测踩过：第一个源
    # 注册成功后清单非空，于是 `connect_source(olist_raw)` 被
    # UNGRANTED_SOURCE 拒 —— 「接入新源」被「这个源还没接入」挡死。
    # 表现是发现覆盖率永远停在第一个源。
    if tool_name in SOURCE_ADMITTING_TOOLS:
        return None
    source_value = str(args.get("source") or "").strip()
    source_key = "source" if source_value else "source_id"
    src = source_value or str(args.get("source_id") or "").strip()
    if not src:
        return None
    granted = st.granted_sources()
    # Case titles are presentation text; source ids are stable identifiers.
    # Accept a case-insensitive display spelling only when it maps to an
    # already human-granted id, and pass the canonical id to the tool.
    canonical = next((item for item in granted
                      if str(item).casefold() == src.casefold()), None)
    if canonical:
        if canonical != src:
            args[source_key] = canonical
        return None
    if not granted:
        return None
    try:
        st.append_event("sources", "UNGRANTED_SOURCE_ATTEMPT",
                        json.dumps({"source": src, "target": _target_of(args)},
                                   ensure_ascii=False))
    except Exception:                                        # noqa: BLE001
        pass
    return (f"[UNGRANTED_SOURCE] 没有人告诉过你 {src} 这个源。"
            f"未经授权去扫一个库不是勤快，是违规 —— 先问人这是谁的系统、"
            f"由他把连接给你。已知的源：{', '.join(sorted(granted))}。")


# silver 轮里的动作。每加一个洗数据的工具，这里同步加一行 ——
# 漏一个就等于给轮级的门开了一扇后窗。
SILVER_TOOLS = {"apply_cleaning_rule"}


def _silver_round_closed(st, tool_name: str):
    """阶段转换是人的决策点，不是 Agent 自行续摊。

    `apply_cleaning_rule` 本来就是 L2（**每条规则**要 Steward 批口径）。
    这里管的是另一个维度：**这一轮该不该开**。两个门缺一不可 ——
    只有前者的话，Agent 做完 bronze 可以自己接着往下洗，
    而「接得差不多了、要不要开清洗轮」是业务判断，不是技术判断。

    **判据：发过阶段提案 = 这个部署在用轮制。**
    与 `source_grants` 同一条判据形状（有人用过 = 生效），
    因为 R1–R4 的形态里根本没有轮的概念，一上来就一律拒会把
    既有的 eval case 全打红，而那些 case 测的是别的东西。

    **这条判据的弱点必须写明**：Agent 不发提案，门就不生效。
    它不是硬边界，靠的是「周报作业会发提案」这个时序 ——
    而周报作业的定义在我们手里，不在模型手里（`.hermes/plugins/data-steward/cron.py`）。
    真正的硬边界要等 M6 的体外监控层，那时才谈得上「轮」是被外部推进的。
    在此之前这是**自觉的取舍**，不是没想到。
    """
    if tool_name not in SILVER_TOOLS:
        return None
    if not st.stage_proposals():
        return None                      # 没用轮制的部署，不改变既有行为
    if st.stage_choice("start_silver") is not None:
        return None
    return (f"[ROUND_NOT_OPEN] 清洗轮还没有人拍板开始，{tool_name} 先不做。"
            f"阶段提案已经发出去了 —— 等负责人在三个选项里点一个。"
            f"**不要重试**，去做别的不受阻塞的事。")


def _quiet_period(st, tool_name: str, level):
    """阶段报告发出去之后，在人拍板之前**不再发起新动作**。

    「到达终态 → 出阶段报告 → 安静下来」是 case 的 stop 段
    （acme_full_v2.md §2）。**停得下来也是能力** —— 跟停止点判据同源：
    下一步需要的判断不在当前上下文里，那就别自己往下走。

    挡的是**会产生新审批 / 新接入的动作**（L2 以上）。L0/L1 照旧放行：
    读元数据、出报告、发下一份提案都在静默期里合法 ——
    `propose_stage_decision` 正是打破静默的那条路，把它也挡了就死锁了。

    判据落在一条**事件**上（`ROUND_CLOSED`，由 `ops/stage-report.py --send` 写），
    不是让门禁自己再算一遍轮的状态：闸门读的量必须真的有人写，
    两处各算一遍必然漂移。没有那条事件 = 这个部署没在用轮制，行为不变。
    """
    if level < Level.L2:
        return None
    closed = st.round_closed_at()
    if closed is None:
        return None                      # 没宣告过结束的部署，行为不变
    decided = st.last_stage_decision_at()
    if decided is not None and decided >= closed:
        return None                      # 人已经拍板了下一步，静默期结束
    return (f"[ROUND_CLOSED] 这一轮已经出过阶段报告了，在负责人决定下一步之前"
            f"不再发起新动作 —— {tool_name} 先不做。"
            f"**不要重试**。要推进请等阶段提案的回复。")


def _target_of(args: dict) -> str:
    """留痕用：把这次调用的对象也记下来，方便复盘是哪一步跑偏的。"""
    return str(args.get("table") or args.get("asset") or "")


def _public_sql_args(args: dict) -> dict:
    """去掉只允许 gate 注入的内部字段，防止模型自己声明已批准。"""
    return {k: v for k, v in dict(args or {}).items()
            if not str(k).startswith("_sql_gate_")}


# 「这件事已经做过了」的时间窗。与 `tools._recently_synced` 同一个数 ——
# 两处不同的话，会出现门禁放行、工具却说「刚接过没有重接」的组合，
# 那时人已经点过链接了。
RECENT_H = 6.0


def _epoch(value) -> float:
    """Normalize SQLite epoch values and PostgreSQL datetime values."""
    return value.timestamp() if hasattr(value, "timestamp") else float(value)


def _already_done(st, tool_name: str, args: dict):
    """做完的事不要再发审批。**拦在建票之前，不是拦在执行之前。**

    实测撞到的：一轮演练里 `customers` / `orders` / `order_details`
    各被申请了 **3 次** —— 每次 cron 唤醒模型都重新规划一遍，
    把接过的表再报一遍。工具自己是幂等的（「6 分钟前刚接过，没有重接」），
    但那句话是**人点完链接之后**才出现的：人已经白点了 9 次。

    工具侧那份幂等判断留着（它挡住的是别的入口），这里挡的是**打扰**。
    照铁律 1：限制写在 hook 里，不写在 prompt 里 —— 已经试过在
    `list_source_tables` 的输出里标「已接入」提醒它，它照样重报。
    """
    if tool_name in ("ingest_table", "full_refresh"):
        if tool_name == "full_refresh":
            return None                  # 刷新的语义就是「明知有也要重来」
        src = str((args or {}).get("source") or "").strip()
        tbl = str((args or {}).get("table") or "").strip()
        if not (src and tbl):
            return None
        said = _recent_sync(st, f"{src}.{tbl}")
        if said:
            return (f"[ALREADY_DONE] {said} 没有发审批 —— "
                    f"人点一次链接是一次打扰，而这件事已经做完了。")
        return None
    if tool_name == "connect_source":
        sid = str((args or {}).get("source_id") or "").strip()
        dsn = str((args or {}).get("dsn") or "").strip()
        if not sid or not _source_live(st, sid):
            return None
        if dsn and _identity_of(dsn) not in _registered_identity(st, sid):
            return None              # 换了账号：那是新动作，照常走审批
        return (f"[ALREADY_DONE] {sid} 已经接进来了，凭证也在。"
                f"直接用 list_source_tables / ingest_table 往下做；"
                f"要换账号才需要重新接入（那时把新连接串带上）。")
    return None


def _recent_sync(st, asset: str):
    """这张表是不是刚同步过。返回一句人话或 None。"""
    from .approvals import rows_of
    try:
        rows = rows_of(st, "SELECT last_synced_at, row_count FROM sync_state"
                           " WHERE asset = {0}", (asset,))
    except Exception:                                        # noqa: BLE001
        return None
    if not rows or not rows[0][0]:
        return None
    age_h = (time.time() - _epoch(rows[0][0])) / 3600.0
    if age_h > RECENT_H:
        return None
    ago = f"{age_h:.1f} 小时前" if age_h >= 1 else f"{max(1, int(age_h * 60))} 分钟前"
    return (f"{asset} {ago}刚接过（{rows[0][1] or 0:,} 行）。"
            f"要刷新数据用 full_refresh；只是想看内容的话它已经在 "
            f"iceberg.bronze 里，直接 sql_query（plane=lake）。")


def _registered_identities(st, source_id: str) -> set:
    """这个源登记过哪些身份（user@host:port/db，**不含口令**）。

    **只读 identity 列，绝不读 dsn。** Agent 对 `source_secrets` 只有
    identity 的列级 SELECT；从前这里读的是 dsn，于是每次都抛
    `InsufficientPrivilege`，被 `except: return False` 吞成「这个源没接入」——
    `_already_done()` 的 connect 分支永不触发，同一个动作被反复发审批，
    人批一张模型生一张（R6 真实演练实测，四张同 action_hash 的票）。

    所以这里**不吞异常**：读不到就是门禁坏了，要能看见，不能装作没接入。
    """
    from .approvals import rows_of
    rows = rows_of(st, "SELECT identity FROM source_secrets WHERE source_id = {0}",
                   (source_id,))
    return {str(r[0]) for r in rows if r and r[0]}


def _source_live(st, source_id: str) -> bool:
    """这个源有没有已经登记好、且没被证明连不通的凭证。"""
    live = _registered_identities(st, source_id) - _failed_identities(st, source_id)
    return bool(live)


def _registered_identity(st, source_id: str) -> set:
    return _registered_identities(st, source_id)


def _failed_identities(st, source_id: str) -> set:
    from .approvals import rows_of
    try:
        rows = rows_of(st, "SELECT payload FROM events WHERE"
                           " kind='SOURCE_CONNECT_FAILED' ORDER BY seq DESC LIMIT 50")
    except Exception:                                        # noqa: BLE001
        return set()
    out = set()
    for (pl,) in rows:
        try:
            d = json.loads(pl or "{}")
        except Exception:                                    # noqa: BLE001
            continue
        if d.get("source_id") == source_id and d.get("identity"):
            out.add(d["identity"])
    return out


def _args_changed(st, approval_id, args: dict) -> bool:
    """这张待批票里存的参数，和模型这次给的，是不是不一样。"""
    from .approvals import rows_of
    try:
        rows = rows_of(st, "SELECT args_json FROM approvals WHERE id = {0}",
                       (approval_id,))
        if not rows:
            return False
        raw = rows[0][0]
        old = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except Exception:                                        # noqa: BLE001
        return False
    return old != dict(args or {})


def _connect_without_dsn(st, tool_name: str, args: dict):
    """`connect_source` 拿不到可用的连接串时，**拒绝，且不发审批**。

    实测撞到的那次：dba 给的账号在这个库上没权限，接入失败；模型于是
    调 `connect_source(source_id="northwind")` 想重试一次 —— 没带 dsn。
    门禁按老路给它开了一张新审批票，票里只有一个 source_id。

    两个后果都不好：

    - 人收到一封「请批准接入 northwind」，批了也没用 —— 里面没有新账号；
    - 批下来之后 handler 会从旧审批里取回**那份已经证明连不通的** dsn，
      于是拿同一份坏账号再连一次。而模型刚跟人说过「不会反复重试」。

    所以这一步的判断是：**有没有可用的连接串**。没有就直接退回，
    让模型去问人要 —— 那本来就是唯一的出路。
    """
    if tool_name != "connect_source":
        return None
    if str((args or {}).get("dsn") or "").strip():
        return None                      # 带了新连接串，正常走审批
    sid = str((args or {}).get("source_id") or "").strip()
    if not sid:
        # **缺参数不归门禁管。** CLAUDE.md：输入校验留在对应代码里，
        # 门禁只判授权。在这里抢着报「需要 source_id」，会把一次
        # 参数写错的调用说成授权问题，而真正的报错在 handler 里写得更准。
        return None
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "services"))
        usable = _usable_dsn_exists(st, sid)
    except Exception:                                        # noqa: BLE001
        usable = False
    if usable:
        return None                      # 有已批准且没失败过的，恢复照走
    return (f"[NEED_DSN] 没有可用的 {sid} 连接串："
            f"你没带 dsn，已批准的那份也已经证明连不通。"
            f"**先去问对方要新的连接信息**，拿到了再调这个工具。"
            f"现在发审批没有意义 —— 票里没有新账号，人批了也接不上。")


def _usable_dsn_exists(st, source_id: str) -> bool:
    """这个源上有没有「已批准、且没被证明连不通」的连接串。

    与 `tools._dsn_from_approval` 是同一条判断 —— 那边取值、这边只问有没有。
    两处都读 `SOURCE_CONNECT_FAILED` 事件，口径一致。
    """
    from .approvals import rows_of
    try:
        def _json_obj(raw):
            return json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})

        approved = [_json_obj(r[0]) for r in rows_of(
            st, "SELECT a.args_json FROM approvals a JOIN decisions d"
                " ON d.approval_id = a.id WHERE a.tool_name='connect_source'"
                " AND d.decision='approve' ORDER BY d.decided_at DESC LIMIT 20")]
        bad = {d.get("identity") for d in
               (_json_obj(r[0]) for r in rows_of(
                   st, "SELECT payload FROM events WHERE"
                       " kind='SOURCE_CONNECT_FAILED' ORDER BY seq DESC LIMIT 50"))
               if d.get("source_id") == source_id}
    except Exception:                                        # noqa: BLE001
        return False
    return any(_identity_of(str(d["dsn"])) not in bad for d in approved
               if d.get("source_id") == source_id and d.get("dsn"))


def _link_not_confirmed(st, args: dict):
    """`answer_with_link` 用的连接必须是**人确认过**的（闭环 C）。

    这条检查在这里、不在工具里，是因为铁律 1：写在工具里就是「模型自觉」，
    而这正是最容易自觉不了的地方 —— 一条 `inferred` 的连接照样 JOIN 得出
    结果，输出看起来跟真的一模一样。

    实测过它有多像：`crm_customer.customer_id = crm_contact.id` 是错的连法，
    但「有对接人的客户数」两种连法都答 40，「上海的」都答 24。
    **答案对不对，从答案本身看不出来。**
    """
    asset = str(args.get("asset") or "").strip()
    key = str(args.get("link_key") or "").strip()
    if not (asset and key):
        return ("[LINK_REQUIRED] answer_with_link 必须指明用的是哪条连接"
                "（asset + link_key）—— 跨表结论要说得清凭什么连。")
    try:
        rows = st.catalog(asset=asset, kind="link")
    except Exception as e:                                   # noqa: BLE001
        # 查不到就拒。**不要 fail-open** —— 「档案读不出来」和
        # 「这条连接人批过」是两回事，混起来等于没有这道门。
        return f"[LINK_UNVERIFIABLE] 读不到连接档案，已按 fail-closed 拒绝：{type(e).__name__}"
    cur = [r for r in rows if r["asset"] == asset and r["key"] == key]
    if not cur:
        return (f"[LINK_UNKNOWN] {asset} 上没有 {key} 这条连接。"
                f"先用 propose_link 找候选，再请人确认。")
    status = cur[-1]["status"]
    if status == "confirmed":
        return None
    if status == "refuted":
        why = cur[-1].get("value")
        return (f"[LINK_REFUTED] 这条连接已经被人否定过：{str(why)[:120]}。"
                f"换一条连法（会产生新的候选），不要重复用它。")
    return (f"[LINK_NOT_CONFIRMED] {asset} · {key} 目前还是「{status}」，"
            f"不是人确认过的事实。推断连得出结果，但那个结果没人担保 —— "
            f"先用 confirm_link 让人拍一次。")


def _sql_guard(args: dict, task_id: str = "", st=None):
    """before_sql：复用 Connector 的 AST 准入，再按 SQL 内容动态升级审批。

    两处实现同一规则必然漂移——之前 gate 与 Connector 各有一份关键字判断，
    改了一处忘了另一处就出安全缺口。现在统一到 `connector._admit`：
    AST 解析，能挡住注释分隔、子查询、CTE 写操作、多语句等关键字挡不住的手法。
    """
    clean_args = _public_sql_args(args)
    sql = (clean_args.get("sql") or "").strip()
    plane = str(clean_args.get("plane") or "source").lower()
    if plane not in ("source", "lake"):
        return {"action": "block", "message": f"[SQL_REJECTED] 未知 SQL plane: {plane!r}"}
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "services"))
        import connector
    except Exception:
        # Connector 不可用时保守拒绝——SQL 是最危险的入口，不做无护栏放行
        return {"action": "block", "message": "SQL 准入组件不可用，已按 fail-closed 拒绝。"}

    try:
        review = connector.review_sql(sql, model_generated=True, plane=plane)
    except Exception as e:
        return {"action": "block",
                "message": f"[SQL_GUARD_ERROR] SQL 准入异常，已按 fail-closed 拒绝：{e}"[:220]}
    if review.action == "reject":
        return {"action": "block", "message": f"[SQL_REJECTED] {review.message}"[:220]}

    checked_args = {**clean_args, "plane": plane, "sql": review.sql}
    # 手动模式：**每条查询也要人点头**。写在这里而不是靠 `effective` ——
    # sql_query 走的是这条独立的内容级准入，上面那次提级它根本走不到。
    # 顺序也是刻意的：**先过 AST 校验再要票**，非法 SQL 直接拒，
    # 不该为一条注定被拒的语句去打扰人。
    manual = manual_approver()
    if review.action == "needs_approval" or manual:
        # 内容级审批本来就点了名（大表 JOIN → owner），保留它；
        # 纯粹因为手动模式才要批的，才发给手动模式指定的那个人。
        # **`SQLReview.approver_role` 默认就是 "owner"**（连 allow 的那份也是），
        # 所以不能直接 `or` —— 那样手动模式下每条轻量查询都会去打扰 owner，
        # 而设 MANUAL_MODE 的人以为自己接管了全部。
        approver_role = (review.approver_role if review.action == "needs_approval"
                         else "") or manual or "owner"
        why = review.message or f"手动模式：每条 SQL 先过 {approver_role}"
        if st is None:
            return {"action": "block",
                    "message": f"[SQL_APPROVAL_REQUIRED] {why}"[:220]}
        h = st.action_hash("sql_query", checked_args)
        if st.is_denied(h):
            return {"action": "block",
                    "message": "[DENIED] sql_query 已被拒绝，不会重复发起审批。"}
        over = _wip_exceeded(st, approver_role)
        if over:
            return {"action": "block", "message": over}
        tok = st.find_valid(h, task_id)
        if not tok:
            aid, created = st.request(task_id, h, "sql_query",
                                      json.dumps(checked_args, ensure_ascii=False),
                                      approver_role)
            if created:
                _notify_async(aid, "sql_query", checked_args, approver_role)
            return {"action": "block",
                    "message": (f"[PENDING_APPROVAL] 这条 SQL 需要人先看一眼，"
                                f"已向 {approver_role} 发起审批（id={str(aid)[:8]}）。"
                                f"原因：{why}。审批通过后再执行。")}
        st.consume(tok[0])
        checked_args["_sql_gate_approved"] = True
        return {"action": "modify", "args": checked_args}

    if checked_args != dict(args or {}):
        return {"action": "modify", "args": checked_args}
    return None


def resolve_approver_role(st, approver_role, args):
    """把 policy 里的粗粒度角色补成带域的具体角色。

    `policy.py` 只能写 `owner`——它不知道有哪些业务域；
    而真实角色是 `owner:fin` / `owner:crm`（readme 10.4 绑角色不绑人）。

    **三级解析，顺序有讲究：**

        ① 记忆层记过归属  →  用它（人确认过的事实，最可信）
        ② 表名前缀能推断  →  用它（确定性信号，但只是约定）
        ③ 都不行          →  回退粗粒度角色（宁可发给上一级，也不要发丢）

    第 ① 级是 LLM 在环演练逼出来的：northwind / olist 的表没有域前缀，
    全都落到 ② 推断不出、③ 兜底给同一个人，而那个人**正确地回了
    「这不是我管的表」**——连着好几天。兜底本身没错，错在它是唯一的依据：
    人一旦告诉过我们归属，就不该再靠表名去猜。
    """
    if not approver_role or ":" in approver_role:
        return approver_role
    table = str(args.get("table") or args.get("source") or "")

    # ① 人确认过的归属。存的是角色名（owner:fin）或直接是邮箱。
    for asset in _asset_keys(args):
        try:
            known = st.known(asset, OWNERSHIP_KEY)
        except Exception:                                    # noqa: BLE001
            known = None
        if not known:
            continue
        val = (known.get("value") if isinstance(known, dict)
               else known[0] if isinstance(known, (list, tuple)) else known)
        val = str(val or "").strip()
        if val.startswith(approver_role + ":"):
            return val
        if "@" in val:
            # 记的是人不是角色：造一个以资产命名的角色，让「跟角色不跟人」照样成立
            role = f"{approver_role}:{_slug(table)}"
            try:
                if not st.resolve_role(role):
                    return approver_role     # Agent 无权指派角色，只能回退
                return role
            except Exception:                                # noqa: BLE001
                return approver_role

    # ② 表名前缀
    domain = table.split(".")[-1].split("_")[0].lower() if table else ""
    if domain:
        specific = f"{approver_role}:{domain}"
        try:
            if st.resolve_role(specific):
                return specific
        except Exception:                                    # noqa: BLE001
            pass

    # ③ 兜底
    return approver_role


OWNERSHIP_KEY = "ownership"


def _slug(table):
    return "".join(c if c.isalnum() else "_" for c in table.split(".")[-1]).lower()


def _asset_keys(args):
    """同一张表可能以几种写法被记过归属，都试一遍。

    `source.table` 是标准写法，但演练与真实调用里也出现过只写表名的。
    多试两个键比要求调用方统一写法便宜。
    """
    t = str(args.get("table") or "")
    src = str(args.get("source") or args.get("source_id") or "")
    out = []
    if src and t:
        out.append(f"{src}.{t}")
    if t:
        out.append(t)
    if src and not t:
        out.append(src)
    return out


def _wip_exceeded(st, approver_role):
    """在办上限（readme 10.7）。

    Owner 每周只有 2–3 小时（docs/industry-context.md），
    一次给他 30 件待办等于什么也批不了。
    超限不是等待，而是让 Agent 转去做不需要审批的工作。
    """
    # 手动模式下**默认值放宽**：人本来就是要逐条看的那个队列，
    # 而这时几乎每个动作都要批（连出站的信也要）。仍然是 3 件的话，
    # Agent 第四步就停下，理由还是「别人待办太多」—— 在手动模式里
    # 那句话是假的，而它长得像门禁正常工作。
    # **显式设了 env 仍然以 env 为准**：要限流的人照样限得住。
    manual = bool(manual_approver())
    per = int(os.environ.get("PER_PERSON_WIP_LIMIT") or (50 if manual else 3))
    glob = int(os.environ.get("GLOBAL_WIP_LIMIT") or (200 if manual else 20))
    try:
        if st.open_count() >= glob:
            return (f"[WIP_LIMIT] 全局在办已达上限 {glob} 件，暂不发起新事项。"
                    f"请先推进其他不受阻塞的任务线。")
        # 按**角色**计数：approvals.approver 存的是角色（readme 10.4 绑角色不绑人）。
        # 解析成真人再计数会永远匹配不上——换人时在办事项也不该被清零。
        if st.open_count(approver_role) >= per:
            who = st.resolve_role(approver_role) or approver_role
            return (f"[WIP_LIMIT] {who} 当前已有 {per} 件待办，暂不发起新事项。"
                    f"请先推进其他不受阻塞的任务线。")
    except Exception:
        return None                       # 计数失败不阻断正常审批
    return None


def _runs():
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "services"))
    import runs
    return runs


def _find_by_fingerprint(tool_name, args):
    """按动作指纹在登记表里找「同一件事」的那条线。

    恢复发生在**新会话**里：cron 唤醒的 agent run 有新的 task_id，
    而原来那条线是用旧会话的 id 记的。M2 已经定过：批的是「接入 shippers」
    这件事，票据只绑动作指纹、不绑会话 —— 登记表得按同一条原则找线，
    否则恢复成功后原线永远挂在 waiting_human，monitor 每分钟都会为它
    白白唤醒一次模型。
    """
    st = store()
    h = st.action_hash(tool_name, dict(args or {}))
    runs = _runs()
    for status in ("waiting_human", "running"):
        for r in runs.by_status(status):
            if (r["kind"] == tool_name and isinstance(r["params"], dict)
                    and st.action_hash(tool_name, r["params"]) == h):
                return r
    return None


def _track_close(task_id, tool_name, status, note="", args=None):
    """这条线走完了 —— 或者被拒了。

    **只在这次调用的工具就是这条线的 kind 时才收尾。** 一条线上会调好几个
    工具（先 profile 再 ingest），随便哪个成功都收尾的话，线会在真正的动作
    发生之前就变成 done —— 那是最难发现的一种假绿：状态栏好看，lake 里空的。

    task_id 对不上时按动作指纹再找一次：恢复跑在新会话里，
    线却是旧会话记的（见 `_find_by_fingerprint`）。
    """
    if not task_id:
        return
    runs = _runs()
    r = runs.get(task_id)
    if (not r or r["kind"] != tool_name
            or r["status"] not in ("running", "waiting_human")):
        r = _find_by_fingerprint(tool_name, args) if args is not None else None
        if not r:
            return
    runs.finish(r["run_id"], status, (note or "")[:400])


def _track_suspend(task_id, approval_id, tool_name, args, note):
    """把「这条线在等谁」记进任务登记表。

    **观察者：不改变放行与否。** 和旁边的 `_notify_async` 同级 ——
    一个告诉人，一个告诉登记表。铁律 1 禁的是把**限制**散出去，
    不是禁止记录发生了什么。

    为什么必须在这里记：门禁一拦，工具 handler 根本不跑。
    以前记账写在 `pipelines/ingest_table.py` 的 `as_run` 包装里，
    那个包装是**驱动脚本伸出去的胳膊**；换成 Hermes 调工具之后
    没有任何人记这条线，于是「16 条线等人 15」这类数字全部落空 ——
    不是 Agent 没干，是没人记账（docs/restructure.md 6.5）。

    `task_id` 就是 run_id：Hermes 的一次会话 = 一条线。
    """
    if not task_id:
        return
    runs = _runs()

    # **先按指纹找，再谈会话。** 同一件事可能已经有线了 —— 上个会话挂起的、
    # 或被 WIP 挡回的。找到就复用，不开第二条。
    prev = _find_by_fingerprint(tool_name, args)
    if prev:
        runs.suspend(prev["run_id"], approval_id,
                     {"stage": "gate", "tool": tool_name}, (note or "")[:400])
        return

    # 新的一条线。run_id 优先用会话 id（一问一答的常见形态就是一会话一条线），
    # **但会话 id 被别的动作占了时必须另开一条**：一次会话里接五张表是
    # 五条线，不是一条。早先这里是「task_id 已存在就直接 suspend」，
    # 于是第二张表往后全部悄悄合并进第一条线 —— 登记表里看着只发起了
    # 一个动作，而 lake 里其实挂了五个待批。实测大 case 时 18 张表只记下 8 条，
    # 就是这么丢的。线的身份是**动作指纹**，会话只是它默认的名字。
    rid = task_id if not runs.get(task_id) else st_action_id(tool_name, args)
    if not runs.get(rid):
        runs.create(tool_name, dict(args or {}), run_id=rid,
                    note=(note or "")[:200])
    runs.suspend(rid, approval_id,
                 {"stage": "gate", "tool": tool_name}, (note or "")[:400])


def st_action_id(tool_name, args) -> str:
    """同会话第二条线的 run_id：**就用动作指纹本身**。

    随机 id 也行，但用指纹的好处是同一动作永远算出同一个 id ——
    即使 `_find_by_fingerprint` 因为状态不在查询范围内而没找到，
    这里也不会凭空造出第二条。
    """
    return store().action_hash(tool_name, dict(args or {}))[:32]


def _notify_async(approval_id, tool_name, args, approver_role):
    """发审批邮件。

    在后台线程里发：pre_tool_call 有超时上限，超时会 fail closed，
    不能让 SMTP 往返拖垮 hook。邮件只是通知，请求本身已经落库。

    ponytail: R1 用线程 + 失败记 event log。
    TODO(R4): 换成 outbox 表 + worker —— 催办与逐级升级本来就需要重扫未决请求。
    """
    import threading

    def _send():
        # SQLite 连接不能跨线程复用 —— 后台线程必须建自己的连接。
        # 这个坑只在真正异步发信时才暴露出来。
        st = open_store(readonly=True, init_schema=False)
        try:
            import sys as _s
            _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "services"))
            import notify
            n = notify.get()
            # 角色 → 当前持有人（readme 10.4）；未配置角色表时回退到 .env
            to = (st.resolve_role(approver_role)
                  or notify.cfg(f"MAIL_{approver_role.upper()}")
                  or notify.cfg("MAIL_OWNER") or "")
            # `endswith`：手动模式下通道被闸门包了一层（`hold:email`），
            # 写死相等的话这条「没配收件人就别发」的分支会失效，
            # 于是拿空收件人去发信，报错长得像 SMTP 坏了。
            if not to and n.name.endswith("email"):
                st.append_event(approval_id, "MAIL_SKIPPED", "未配置收件人")
                return
            target = args.get("table") or args.get("source") or json.dumps(args, ensure_ascii=False)
            n.send_approval(to, approval_id, tool_name, target,
                            "该动作需要你确认后才会执行。", to or approver_role)
            st.append_event(approval_id, "MAIL_SENT", f"{n.name}:{to}")
        except Exception as e:
            # 发信失败不影响拦截 —— 动作依然不会执行，只是通知没送达
            st.append_event(approval_id, "MAIL_FAILED", str(e)[:200])
        finally:
            st.close()

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    _PENDING_MAILS.append(t)


# 在飞的发信线程。**进程退出前必须等它们一下。**
#
# 这些线程是 daemon：进程一退，还没发完的直接被杀 —— 审批请求落了库、
# 门禁也挡住了，但**人永远收不到那封信**，于是那条线永远挂在
# waiting_human 上等一个不会来的决定。
#
# 长驻进程里看不出问题，`hermes -z` 这种一次性进程里必然踩到：
# 实测大 case 时 6 封发出去了、后面全丢，表现是「只有前几个源接进来了」。
# 又是一次静默 —— 门禁工作正常，账也记了，就是信没出去。
_PENDING_MAILS = []


def _drain_mails(timeout: float = 8.0):
    """等在飞的发信线程收尾。**总时长有上限**，不能让发信拖住退出。"""
    import time as _t
    deadline = _t.time() + timeout
    for t in list(_PENDING_MAILS):
        left = deadline - _t.time()
        if left <= 0:
            break
        t.join(timeout=left)
    _PENDING_MAILS.clear()


import atexit                                                # noqa: E402
atexit.register(_drain_mails)


# --------------------------------------------------------------------------
# post_tool_call —— 观察者：event log + 负载记账
# --------------------------------------------------------------------------
def audit(tool_name: str, args: dict, result=None, status: str = "", **kwargs):
    task_id = kwargs.get("task_id", "")
    st = store()
    st.append_event(task_id, f"TOOL_{status or 'DONE'}",
                    json.dumps({"tool": tool_name}, ensure_ascii=False))
    # 工具真跑完了 = 这条线的动作落地了。**门禁一侧记开头，这里记结尾** ——
    # 否则被批准之后线会一直挂在 waiting_human 上，看起来像没人推动。
    #
    # **但「跑完了」不等于「做成了」。** 我们的工具用**返回值**报错
    # （给模型一句人话，而不是抛异常炸掉整轮），Hermes 那边看到的
    # status 一律是成功 —— 于是失败的动作也被收成 done。
    # 实测撞过：`connect_source` 因为拿不到 dsn 返回了一句错误，
    # 线却变成 done，而 `source_grants` 里什么都没有：状态栏好看，
    # 库里是空的 —— 本项目第 1 号坑的又一次变形。
    failed = _looks_failed(result)
    if (status or "DONE").upper() in ("", "DONE", "OK", "SUCCESS"):
        try:
            _track_close(task_id, tool_name, "failed" if failed else "done",
                         str(result or "")[:200], args)
        except Exception:                                    # noqa: BLE001
            pass
        if not failed:
            try:
                _record_provenance(st, tool_name, args, result)
            except Exception:                                # noqa: BLE001
                pass                     # 记账失败不改变已经发生的事


# 哪些工具改变了一张资产的来历。**加一个改数据的工具 = 加一行。**
#
# 写在 post_tool_call 这一处，而不是散进每个 handler：血缘要么全记、
# 要么不记，散着写必然漏 —— 漏掉的那条恰好是审计要问的那条。
# 值是 (事件名, 从 args 里怎么取资产名)。
_PROVENANCE_EVENTS = {
    "connect_source":      ("source_registered", lambda a: a.get("source_id")),
    "ingest_table":        ("ingested", lambda a: _asset_of(a)),
    "ingest_export":       ("ingested_from_file",
                            lambda a: f'{a.get("saas_source")}.{a.get("table")}'),
    "apply_cleaning_rule": ("cleaned", lambda a: _asset_of(a)),
    "publish_gold":        ("published", lambda a: a.get("silver_table")),
    "grant_read":          ("granted", lambda a: a.get("asset")),
    "define_semantics":    ("semantics_defined", lambda a: a.get("asset")),
    "classify_asset":      ("classified", lambda a: a.get("asset")),
    "full_refresh":        ("refreshed", lambda a: _asset_of(a)),
    # 下面三个还没实现（见 tests/test_toolset_whitelist 的 NOT_YET_IMPLEMENTED），
    # 但**记什么账属于设计**：等补实现时不必再想一遍，也不会漏掉血缘。
    "confirm_column_mapping": ("column_mapping_confirmed",
                               lambda a: a.get("asset")),
    # 「这两张表凭什么能连」是这张表经历里必须记的一步 ——
    # 后面所有跨表结论都建在它上面，审计问的往往正是它。
    "confirm_link":        ("link_confirmed", lambda a: a.get("asset")),
    "connect_saas_control_plane": ("control_plane_connected",
                                   lambda a: a.get("source_id")),
    "dump_saas_permissions": ("permissions_dumped", lambda a: a.get("source_id")),
}


def _asset_of(a: dict) -> str:
    src = a.get("source") or a.get("source_id") or ""
    tbl = a.get("table") or ""
    return f"{src}.{tbl}" if src and tbl else (tbl or src)


def _record_provenance(st, tool_name, args, result):
    """把这次动作记进资产台账。**观察者：写不进去也不影响动作本身。**

    只记成功的动作 —— 失败的已经由 `_track_close` 记成 failed 的线，
    台账是「这张表经历过什么」，没成的事不该出现在里面。
    """
    spec = _PROVENANCE_EVENTS.get(tool_name)
    if not spec:
        return
    event, pick = spec
    asset = str(pick(args or {}) or "").strip()
    if not asset or asset.startswith("."):
        return
    detail = {k: v for k, v in (args or {}).items()
              if k.lower() not in _SECRET_ARGS}
    # 连接串只留身份，口令一律不进台账 —— 台账是要给审计看的。
    if args.get("dsn"):
        detail["connection_identity"] = _identity_of(args["dsn"])
    detail["result"] = str(result or "")[:300]
    st.record_provenance(asset, event, actor=_actor_of(st, tool_name, args),
                         approval_id=_last_approval(st, tool_name), detail=detail)


# 绝不进台账的参数名。台账是给审计看的，口令不该出现在那里。
_SECRET_ARGS = {"dsn", "password", "passwd", "secret", "token", "api_key"}


def _identity_of(dsn: str) -> str:
    """单一实现放在 approvals 里 —— 写库那一侧和判幂等这一侧必须同一套切分，
    两份各写一遍迟早会漂移成「登记的身份和门禁比对的身份对不上」。"""
    from .approvals import _identity_of_dsn
    return _identity_of_dsn(dsn)


def _actor_of(st, tool_name, args) -> str:
    """谁促成了这次动作。优先记**批准人**（审计问的是这个），
    其次是给出信息的人。"""
    try:
        row = st.db.execute(
            "SELECT d.approver FROM approvals a JOIN decisions d"
            " ON d.approval_id=a.id WHERE a.tool_name=?"
            " ORDER BY d.decided_at DESC LIMIT 1", (tool_name,)).fetchone() \
            if hasattr(st.db, "execute") else None
        if row and row[0]:
            return row[0]
    except Exception:                                        # noqa: BLE001
        pass
    return str((args or {}).get("confirmed_by")
               or (args or {}).get("given_by") or "")


def _last_approval(st, tool_name) -> str:
    try:
        row = st.db.execute(
            "SELECT a.id FROM approvals a JOIN decisions d"
            " ON d.approval_id=a.id WHERE a.tool_name=?"
            " ORDER BY d.decided_at DESC LIMIT 1", (tool_name,)).fetchone() \
            if hasattr(st.db, "execute") else None
        return row[0] if row else ""
    except Exception:                                        # noqa: BLE001
        return ""


# 工具报错时返回值的开头。**约定**：handler 用返回值报错必须这样开头，
# 否则这条线会被当成做成了。加新工具时照着写。
_FAILURE_PREFIXES = ("错误：", "错误:", "失败", "**规则名对不上")
_FAILURE_MARKS = ("失败：", "失败:", "连不上", "未注册", "不可用")


def _looks_failed(result) -> bool:
    """工具是不是用返回值报了错。

    认字符串确实脆，但比「全部当成功」结实得多 —— 后者的失败方式是
    静默的（线变 done、库里空的），前者最多是把一句含「失败」的正常
    输出误判成失败，那会立刻被人看见。**宁可吵，不可静。**
    """
    t = str(result or "").lstrip()
    return (t.startswith(_FAILURE_PREFIXES)
            or any(m in t[:80] for m in _FAILURE_MARKS))


# --------------------------------------------------------------------------
# pre_approval_request —— 观察者：发审批邮件
# --------------------------------------------------------------------------
def notify(command: str = "", description: str = "", session_key: str = "",
           surface: str = "", **kwargs):
    # TODO(R1): 接 Hermes 的 Email 适配器发送带签名 token 的批准链接
    st = store()
    st.append_event(session_key, "APPROVAL_REQUESTED",
                    json.dumps({"surface": surface, "desc": description[:200]},
                               ensure_ascii=False))


def register(ctx):
    """Hermes plugin 入口。"""
    ctx.register_hook("pre_tool_call", gate)
    ctx.register_hook("post_tool_call", audit)
    ctx.register_hook("pre_approval_request", notify)
