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


def get(channel=None) -> Notifier:
    # 进程环境变量优先于 .env —— 否则测试/演练无法临时切通道，
    # 只能去改仓库里的配置文件。
    ch = (channel or os.environ.get("NOTIFY_CHANNEL")
          or E.get("NOTIFY_CHANNEL", "email")).lower()
    if ch == "feishu":
        from .feishu import FeishuNotifier
        return FeishuNotifier()
    if ch == "wecom":
        from .wecom import WecomNotifier
        return WecomNotifier()
    if ch == "outbox":
        from .outbox import OutboxNotifier
        return OutboxNotifier()
    from .email_channel import EmailNotifier
    return EmailNotifier()


def approval_links(approval_id, approver, base=None):
    """两枚一次性签名链接。所有通道共用同一套令牌机制（readme 9.4）。"""
    import sys
    sys.path.insert(0, str(ROOT / "services"))
    import tokens
    b = (base or E.get("APPROVAL_BASE_URL", "http://127.0.0.1:8787")).rstrip("/")
    return (f"{b}/approve?t={tokens.issue(approval_id, 'approve', approver)}",
            f"{b}/deny?t={tokens.issue(approval_id, 'deny', approver)}")
