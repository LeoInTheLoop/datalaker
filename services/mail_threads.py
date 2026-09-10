"""出站邮件 ↔ 任务线的归属登记（readme 10.1 第 1/3/4 层）。

**问题**：一个人可以同时挂着好几条线 —— 王工手上既有「orders 口径」
也有「customers 口径」。他点审批链接的时候归属是精确的，令牌绑死了
`approval_id`；但他**回一封信**说「同意，用 DELIVERED」的时候，
正文里没有任何东西说明这是在回哪一条。

`inbound.resolve_item` 早就定了四层归属阶梯，但前面几层需要发信那一侧
先留下线索，否则一路掉到第 5 层交给模型猜 —— 而猜错了系统还不自知。
这个模块负责留下那三条确定性线索：

  第 1 层  In-Reply-To / References  → `mail_threads.message_id`
  第 3 层  plus-address `+ap-<token>` → `mail_threads.thread_token`
  第 4 层  主题 `[#<token>]`          → 同上，人手动转发也保得住

三条都落空才轮到模型，且必须标 `certain=False`。

**这不是授权路径。** 归属只决定「这封信在说哪条线」，不决定
「这条线可以往下走」—— 后者仍然只看 `decisions`，而正文里写「我同意」
永远不作数（`inbound.decision_still_requires_click`）。所以归属判错的
代价是体验损失，不是授权事故。
"""
from __future__ import annotations

import hashlib
import re
import time

# token 取 12 位十六进制：`inbound.PLUS_RE` 与 `SUBJ_RE` 认的是
# `[0-9a-f-]{6,}`，而 run_id 长这样 `ingest_table-a1b2c3d4` ——
# 直接把 run_id 前几位塞进主题，正则根本不匹配（`ingest_t` 不是十六进制）。
# 所以线的对外标识与线的 id 分开：token 由 run_id 确定性派生，
# 同一条线的每一封信带同一个 token，回哪一封都能绑回同一条线。
TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")


def token_of(run_id: str) -> str:
    return hashlib.sha256(f"thread:{run_id}".encode()).hexdigest()[:12]


def tag_subject(subject: str, token: str) -> str:
    """主题挂上 `[#token]`。已经有了就不重复挂（回信会带上原主题）。"""
    if not token or f"[#{token}]" in (subject or ""):
        return subject or ""
    return f"{subject} [#{token}]"


def plus_address(addr: str, token: str) -> str:
    """`claw@acme.test` → `claw+ap-<token>@acme.test`。

    只用在 `Reply-To` 上，不动 `From` —— 改 From 会打乱 SPF/DKIM 对齐，
    而入站验真（`inbound.sender_authenticated`）正是按 From 域判的。
    """
    if not token or "@" not in (addr or "") or "+" in addr.split("@", 1)[0]:
        return addr or ""
    local, _, domain = addr.partition("@")
    return f"{local}+ap-{token}@{domain}"


# ------------------------------------------------------------------ 存储
def _store(readonly=False):
    # 与 `services/runs.py` 同一处坑：Hermes 自己有个顶层 `plugins` 包，
    # cron 的子进程里它会盖住本项目的命名空间包。
    try:
        from plugins.datasteward_gate.approvals import open_store
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("plugins"):
            raise
        from datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=not readonly)


def _rows(st, sql, params=()):
    # 同 `_store()`：Hermes 的顶层 `plugins` 包会盖住本项目的命名空间包，
    # 这里也得有同一条退路，否则 cron 子进程里读得到库却导不进 helper。
    try:
        from plugins.datasteward_gate.approvals import rows_of
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("plugins"):
            raise
        from datasteward_gate.approvals import rows_of
    return rows_of(st, sql, params)


def _write(st, sql, params=()):
    """按后端类名判，不按 `hasattr` —— 见 `approvals.rows_of` 的注释。"""
    if type(st).__name__ == "PgStore":
        with st.db.cursor() as c:
            c.execute(sql.format("%s"), params)
        return
    st.db.execute(sql.format("?"), params)
    st.db.commit()


def _clean(message_id: str) -> str:
    return (message_id or "").strip().strip("<>")


def record(message_id: str, run_id: str = "", approval_id: str = "",
           to_addr: str = "", subject: str = "", kind: str = "notice") -> bool:
    """记一封已经发出去的信。**失败不能让调用方以为信没发出去。**

    信确实发出去了；记不上只是让这条线以后要靠第 5 层去猜。
    所以这里吞掉异常，但**要留痕** —— 静默丢失才是本项目的一号坑。
    """
    mid = _clean(message_id)
    if not mid:
        return False
    try:
        st = _store()
        try:
            _write(st,
                   "INSERT INTO mail_threads (message_id, thread_token, run_id,"
                   " approval_id, to_addr, subject, kind, sent_at)"
                   " VALUES ({0},{0},{0},{0},{0},{0},{0},{0})"
                   " ON CONFLICT (message_id) DO NOTHING",
                   (mid, token_of(run_id) if run_id else "", run_id or None,
                    approval_id or None, to_addr, (subject or "")[:400],
                    kind, time.time()))
        finally:
            st.close()
        return True
    except Exception as exc:                                 # noqa: BLE001
        try:
            st = _store()
            try:
                st.append_event(run_id or "mailroom", "MAIL_THREAD_RECORD_FAILED",
                                f"{type(exc).__name__}: {str(exc)[:160]}")
            finally:
                st.close()
        except Exception:                                    # noqa: BLE001
            pass
        return False


def run_of_approval(approval_id: str) -> str:
    """票据属于哪条线。发审批信时用它拿 run_id —— 票上本来就记着。"""
    if not approval_id:
        return ""
    try:
        st = _store(readonly=True)
        try:
            r = _rows(st, "SELECT run_id FROM approvals WHERE id = {0}",
                      (approval_id,))
        finally:
            st.close()
        return str(r[0][0]) if r else ""
    except Exception:                                        # noqa: BLE001
        return ""


def run_of_message(message_id: str) -> str:
    """第 1 层：他回的是我们发过的哪一封信。"""
    mid = _clean(message_id)
    if not mid:
        return ""
    try:
        st = _store(readonly=True)
        try:
            r = _rows(st, "SELECT run_id FROM mail_threads WHERE message_id = {0}",
                      (mid,))
        finally:
            st.close()
        return str(r[0][0]) if r and r[0][0] else ""
    except Exception:                                        # noqa: BLE001
        return ""


def run_of_token(token: str) -> str:
    """第 3、4 层：plus-address 或主题里的 token 指向哪条线。"""
    if not TOKEN_RE.match(str(token or "").lower()):
        return ""
    try:
        st = _store(readonly=True)
        try:
            r = _rows(st, "SELECT run_id FROM mail_threads WHERE thread_token = {0}"
                          " ORDER BY sent_at DESC", (token.lower(),))
        finally:
            st.close()
        return str(r[0][0]) if r and r[0][0] else ""
    except Exception:                                        # noqa: BLE001
        return ""


def resolve_run(headers: dict) -> dict:
    """跑完整条阶梯，返回 `{run_id, layer, certain, via}`。

    这是 `inbound.resolve_item` 的生产落点：它只认「有没有协议线索」，
    把线索翻译成**哪一条任务线**是这里的事。

    `run_id` 为空 = 三条确定性线索都没命中。**这时候不要猜** ——
    调用方应当把候选集限定为这个人当前 `waiting_human` 的线再问，
    或者直接回信问是哪一条。
    """
    import inbound

    item = inbound.resolve_item(headers, lookup_by_message_id=run_of_message)
    layer = item.get("layer")
    if layer == 1 and item.get("item_id"):
        return {"run_id": str(item["item_id"]), "layer": 1, "certain": True,
                "via": item.get("via", "")}
    if layer in (3, 4) and item.get("item_id"):
        rid = run_of_token(str(item["item_id"]))
        if rid:
            return {"run_id": rid, "layer": layer, "certain": True,
                    "via": item.get("via", "")}
        # token 认得出格式却查不到线：**说出来**，别退回去当成「没线索」。
        # 典型成因是库被重置过而人回的是上一轮的信。
        return {"run_id": "", "layer": layer, "certain": False,
                "via": f"{item.get('via', '')}（token 不在登记表里）"}
    return {"run_id": "", "layer": item.get("layer", 5),
            "certain": False, "via": item.get("via", "")}


def threads_of(run_id: str) -> list:
    """这条线发过哪些信 —— 排查归属时用。"""
    if not run_id:
        return []
    try:
        st = _store(readonly=True)
        try:
            rows = _rows(st, "SELECT message_id, kind, to_addr, subject, sent_at"
                             " FROM mail_threads WHERE run_id = {0}"
                             " ORDER BY sent_at", (run_id,))
        finally:
            st.close()
        return [{"message_id": r[0], "kind": r[1], "to": r[2],
                 "subject": r[3], "sent_at": float(r[4])} for r in rows or []]
    except Exception:                                        # noqa: BLE001
        return []
