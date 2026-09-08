"""通知通道抽象。

门禁逻辑与通道无关（readme 第 10 节）：归属判定、意图分类、角色化、
WIP 限制都不关心消息是从邮件还是飞书发出去的。
因此通道必须可替换——加一个渠道不应改动 `plugins/datasteward_gate`。

    NOTIFY_CHANNEL=email    邮件（默认）
    NOTIFY_CHANNEL=feishu   飞书交互卡片
    NOTIFY_CHANNEL=wecom    企业微信模板卡片
"""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def env(path=None):
    d = {}
    f = pathlib.Path(path or ROOT / ".env")
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


E = env()


class Notifier:
    """所有通道实现同一组方法。

    每个方法都必须返回 dict（至少含 channel），或抛异常。
    **抛异常表示通知未送达，绝不表示门禁放行**——两者无关（readme 第 9 节）。
    """

    name = "base"

    def send_approval(self, to, approval_id, tool, target, reason, approver):
        raise NotImplementedError

    def send_receipt(self, to, approval_id, decision, tool, target, approver):
        """回执：批准之后告诉他批了什么（readme 10.6）。"""
        raise NotImplementedError

    def send_notice(self, to, subject, body):
        """通用通知：周报、升级、告警。"""
        raise NotImplementedError


def manual_approver() -> str:
    """手动模式的审批人角色；没开就是空串。

    **判据在 `policy.py`，这里只是读它** —— 工具要不要批、信要不要批，
    必须是同一个开关。两处各判一次的话，会出现「工具都要批、信照发」
    这种半截状态，而它看起来一切正常。

    policy 拿不到时（notify 被单独拿去用）退回同一条判据的字面实现：
    宁可多兜一层，也不要在这里静默地把手动模式当成没开。
    """
    try:
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from plugins.datasteward_gate.policy import manual_approver as _m
        return _m()
    except Exception:                                        # noqa: BLE001
        v = os.environ.get("MANUAL_MODE", "").strip()
        if v.lower() in ("", "0", "false", "no", "off"):
            return ""
        return "steward" if v.lower() in ("1", "true", "yes", "on") else v


class HoldNotifier(Notifier):
    """手动模式的出站闸：**对外通知先扣住，等人点头才发。**

    门禁挡的是「Agent 要做什么」，这里挡的是「Agent 要对外说什么」——
    两件事。批准 `connect_source` 不等于批准了那封通报的**正文**，
    而正文是模型写的、没人看过。手动模式要堵的正是这一段。

    **只扣 `send_notice`。** 审批信和回执必须原样发出去：
      · 扣住审批信 = 套娃死锁（要批的那封信本身在等批准），
        于是人永远收不到任何东西，看起来像门禁把系统卡死了；
      · 回执是人自己刚点过的那一下的结果，不是 Agent 在说话。

    放行之后由 **callback 进程**真正发出（`services/approval_callback.py`）：
    它是人这一侧的进程，而这封信本来就是人点头之后才存在的那封。
    """

    def __init__(self, inner: Notifier, approver: str):
        self.inner = inner
        self.approver = approver
        self.name = f"hold:{inner.name}"

    def send_approval(self, to, approval_id, tool, target, reason, approver):
        return self.inner.send_approval(to, approval_id, tool, target,
                                        reason, approver)

    def send_receipt(self, to, approval_id, decision, tool, target, approver):
        return self.inner.send_receipt(to, approval_id, decision, tool,
                                       target, approver)

    def send_notice(self, to, subject, body):
        import json
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from plugins.datasteward_gate.approvals import open_store

        letter = {"to": to, "subject": subject, "body": body}
        st = open_store(readonly=True)
        try:
            aid, created = st.request(
                "mailroom", st.action_hash("send_notice", letter),
                "send_notice", json.dumps(letter, ensure_ascii=False),
                self.approver)
            if created:
                # 审批信走**内层通道**，绕过这道闸 —— 见上面的套娃那段。
                who = (st.resolve_role(self.approver)
                       or cfg(f"MAIL_{self.approver.upper().replace(':', '_')}")
                       or cfg("MAIL_OWNER") or "")
                if who:
                    self.inner.send_approval(
                        who, aid, "send_notice", f"发给 {to}：{subject}",
                        # 让人看到**正文本身**再决定发不发 —— 只写
                        # 「有一封信要发」等于让他闭着眼睛点批准。
                        f"这封信在等你放行，正文如下：\n\n{body[:1200]}",
                        who)
                    st.append_event(aid, "NOTICE_HELD", f"{to} · {subject}"[:180])
                else:
                    # 收件人都解析不出来时**要说出来**：信扣住了、没人被通知，
                    # 这条线会一直挂着，而日志里若什么都没有就查不出来。
                    st.append_event(aid, "NOTICE_HELD_NO_APPROVER",
                                    f"{self.approver} 解析不到邮箱")
        finally:
            st.close()
        return {"channel": self.name, "to": to, "kind": "held",
                "approval_id": aid, "subject": subject}


def get(channel=None, hold=True) -> Notifier:
    # 进程环境变量优先于 .env —— 否则测试/演练无法临时切通道，
    # 只能去改仓库里的配置文件。
    ch = (channel or os.environ.get("NOTIFY_CHANNEL")
          or E.get("NOTIFY_CHANNEL", "email")).lower()
    if ch == "feishu":
        from .feishu import FeishuNotifier
        n = FeishuNotifier()
        who = manual_approver() if hold else ""
        return HoldNotifier(n, who) if who else n
    if ch == "wecom":
        from .wecom import WecomNotifier
        n = WecomNotifier()
        who = manual_approver() if hold else ""
        return HoldNotifier(n, who) if who else n
    if ch == "outbox":
        from .outbox import OutboxNotifier
        n = OutboxNotifier()
    else:
        from .email_channel import EmailNotifier
        n = EmailNotifier()
    # `hold=False` 是**人这一侧的进程**在发信（callback 的确认信、回执）——
    # 那些不该被手动模式扣住，否则人点了链接反而卡在自己的闸门上。
    who = manual_approver() if hold else ""
    return HoldNotifier(n, who) if who else n


def sent_line(res, to: str) -> str:
    """一句人话交代这封信到底出去了没有。

    手动模式下 `send_notice` 返回的是 `kind=held`（信在等人放行）。
    照旧印「已发送至 X」就是假绿 —— 本项目第 1 号坑的又一次变形，
    而这次它出现在运维脚本的输出里，人正是靠那行字判断要不要去催。
    """
    if isinstance(res, dict) and res.get("kind") == "held":
        return (f"已扣在待发队列，等人放行后才会发给 {to}"
                f"（手动模式 · {str(res.get('approval_id', ''))[:8]}）")
    return f"已发送至 {to}"


def cfg(key: str, default: str = "") -> str:
    """读一个通道配置。**进程环境变量优先于 `.env`。**

    与 `get()` 里 NOTIFY_CHANNEL、`_base_url()` 里 APPROVAL_BASE_URL 同一条
    优先级 —— 这是同一个坑的第三处：只读 `.env` 的话，想把 SMTP 指到
    本地模拟邮箱（演练、eval）就只能去改仓库里的配置文件，
    而那份文件是给生产用的。
    """
    v = os.environ.get(key)
    return v if v not in (None, "") else E.get(key, default)


def _base_url(base=None) -> str:
    """回调服务的地址。**进程环境变量优先于 `.env`。**

    与 `get()` 里 NOTIFY_CHANNEL 的优先级保持一致。原先只读 `.env`，
    于是把 callback 起在别的端口的测试/演练**永远拿不到指向它的链接** ——
    链接照旧指向 .env 里那个 8787，而那个端口上可能正跑着 docker 里的
    另一个 callback（不同的库、不同的签名密钥）。表现是「令牌无效」
    或者干脆连接被掐，看着像门禁坏了，其实是信发错了地方。
    """
    return (base or os.environ.get("APPROVAL_BASE_URL")
            or E.get("APPROVAL_BASE_URL", "http://127.0.0.1:8787")).rstrip("/")


def approval_links(approval_id, approver, base=None):
    """两枚一次性签名链接。所有通道共用同一套令牌机制（readme 9.4）。"""
    import sys
    sys.path.insert(0, str(ROOT / "services"))
    import tokens
    b = _base_url(base)
    return (f"{b}/approve?t={tokens.issue(approval_id, 'approve', approver)}",
            f"{b}/deny?t={tokens.issue(approval_id, 'deny', approver)}")


def choice_links(approval_id, approver, options, base=None):
    """三选一：每个选项一枚一次性签名链接。

    与 `approval_links` 同一套令牌机制 —— 点哪枚就是选哪个。
    **正文里写「我选 B」不算数**，理由和审批那边一样：
    正文可以伪造，转发的链接会被误点。
    """
    import sys
    sys.path.insert(0, str(ROOT / "services"))
    import tokens
    b = _base_url(base)
    return [(o, f"{b}/choose?t={tokens.issue(approval_id, o['key'], approver)}")
            for o in options]
