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
import os

from .approvals import Store, open_store
from .policy import HERMES_DANGEROUS, Level, is_declared, lookup

DB_PATH = os.environ.get("DATASTEWARD_DB", os.path.expanduser("~/.datalaker/approvals.db"))
_store: Store | None = None


def store():
    """Agent 侧存储句柄。

    DATASTEWARD_DSN 存在 -> Postgres（生产 / 容器内，列级 GRANT 在此生效）
    否则                  -> SQLite（无 docker 时的本地快测）
    """
    global _store
    if _store is None:
        _store = open_store(readonly=True)       # Agent 侧只读（readme 9.4 机制三）
    return _store


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
        return _gate(tool_name, args, task_id, **kwargs)
    except Exception as e:
        return {"action": "block",
                "message": f"[GATE_ERROR] 治理组件异常，已按 fail-closed 拒绝执行 "
                           f"{tool_name}：{type(e).__name__}: {e}"}


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
    level, approver_role = lookup(tool_name)

    # 预算兜底：只挡消耗型动作，不挡纯读元数据（否则连状态都查不了）
    if level >= Level.L1:
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

    # sql_query 是 L1 工具，但风险不只由工具名决定：同一个工具里，
    # SELECT 10 行和 JOIN 大表不是同一类动作。因此 SQL 走内容级动态准入。
    if tool_name == "sql_query":
        return _sql_guard(args, task_id=task_id, st=st)

    # deny list：拒绝过的动作不再重复发起审批
    if st.is_denied(h):
        return {"action": "block",
                "message": f"[DENIED] {tool_name} 已被拒绝，不会重复发起审批。"
                           f"请改变方案（将产生新的动作指纹）或触发升级。"}

    # 角色补域：owner -> owner:fin（见 resolve_approver_role）
    if approver_role:
        approver_role = resolve_approver_role(st, approver_role, args)

    # WIP 限制（readme 10.7）：不要淹没任何人
    if level >= Level.L2:
        over = _wip_exceeded(st, approver_role or "owner")
        if over:
            return {"action": "block", "message": over}

    # L2 / L3：需要有效票据
    if level >= Level.L2:
        tok = st.find_valid(h, task_id)
        if not tok:
            aid, created = st.request(task_id, h, tool_name,
                                      json.dumps(args, ensure_ascii=False),
                                      approver_role or "owner")
            if created:
                _notify_async(aid, tool_name, args, approver_role or "owner")
            return {"action": "block",
                    "message": f"[PENDING_APPROVAL] 已就 {tool_name} 向 {approver_role} 发起审批"
                               f"（id={aid[:8]}）。审批通过后本任务会被重新唤醒，"
                               f"当前不要重试，请继续处理其他不受阻塞的任务线。"}
        st.consume(tok[0])

    return None      # 放行


def _public_sql_args(args: dict) -> dict:
    """去掉只允许 gate 注入的内部字段，防止模型自己声明已批准。"""
    return {k: v for k, v in dict(args or {}).items()
            if not str(k).startswith("_sql_gate_")}


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
    if review.action == "needs_approval":
        if st is None:
            return {"action": "block",
                    "message": f"[SQL_APPROVAL_REQUIRED] {review.message}"[:220]}
        h = st.action_hash("sql_query", checked_args)
        if st.is_denied(h):
            return {"action": "block",
                    "message": "[DENIED] sql_query 已被拒绝，不会重复发起审批。"}
        approver_role = review.approver_role or "owner"
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
                    "message": (f"[PENDING_APPROVAL] SQL 查询可能增加源系统负担，"
                                f"已向 {approver_role} 发起审批（id={aid[:8]}）。"
                                f"原因：{review.message}。审批通过后再执行。")}
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
    per = int(os.environ.get("PER_PERSON_WIP_LIMIT", "3") or 3)
    glob = int(os.environ.get("GLOBAL_WIP_LIMIT", "20") or 20)
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
                  or notify.E.get(f"MAIL_{approver_role.upper()}")
                  or notify.E.get("MAIL_OWNER") or "")
            if not to and n.name == "email":
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

    threading.Thread(target=_send, daemon=True).start()


# --------------------------------------------------------------------------
# post_tool_call —— 观察者：event log + 负载记账
# --------------------------------------------------------------------------
def audit(tool_name: str, args: dict, result=None, status: str = "", **kwargs):
    st = store()
    st.append_event(kwargs.get("task_id", ""), f"TOOL_{status or 'DONE'}",
                    json.dumps({"tool": tool_name}, ensure_ascii=False))


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
