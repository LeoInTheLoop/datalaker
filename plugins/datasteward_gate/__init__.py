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
from .policy import Level, lookup

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

    # L4：永不自动执行
    if level >= Level.L4:
        return {"action": "block",
                "message": f"[L4] {tool_name} 永不自动执行，需线下双人批准后由人工操作。"}

    # deny list：拒绝过的动作不再重复发起审批
    if st.is_denied(h):
        return {"action": "block",
                "message": f"[DENIED] {tool_name} 已被拒绝，不会重复发起审批。"
                           f"请改变方案（将产生新的动作指纹）或触发升级。"}

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

    # before_sql：源系统查询护栏（readme 8.1）
    if tool_name == "sql_query":
        return _sql_guard(args)

    return None      # 放行


def _sql_guard(args: dict):
    sql = (args.get("sql") or "").strip()
    low = sql.lower()

    if " join " in low:
        return {"action": "block",
                "message": "源系统上不允许 join —— 请分别抽取到 bronze 后在 lake 中关联。"}

    head = low.split(None, 1)[0] if low else ""
    if head not in ("select", "show", "describe"):
        return {"action": "block", "message": "仅允许 SELECT / SHOW / DESCRIBE。"}

    if " limit " not in low:
        return {"action": "modify",
                "args": {"sql": sql.rstrip("; ") + " LIMIT 1000"}}
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
            to = (notify.E.get(f"MAIL_{approver_role.upper()}")
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
