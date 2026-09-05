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
