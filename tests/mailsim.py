"""GreenMail 模拟邮箱的收发工具（P2 驱动反转用，纯标准库）。

驱动脚本用它扮演王姐、李哥：SMTP 发进 GreenMail，Hermes 按 IMAP 轮询收；
判分侧也用 IMAP 直接读任意收件箱（auth.disabled：登录随便、收件人即建）。
不用 GreenMail 的 REST API —— 收发两个协议 stdlib 就够，少一个依赖面。

**Authentication-Results 由剧本来加。** 真实世界这个头是收件基础设施
prepend 的（M4 的发件人验真只认它）；模拟环境里剧本就是基础设施：
合法 persona 给 pass，冒充者给 fail 或干脆不给 —— 验真逻辑被原样考到。
这也是选 GreenMail 不用真 Gmail 的核心理由：真 Gmail 禁止伪造 From，
最关键的冒充场景反而测不了。

起服务：cd infra && docker compose --profile mail up -d greenmail
"""
import email
import email.policy
import imaplib
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

HOST = os.environ.get("MAILSIM_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAILSIM_SMTP_PORT", "3025"))
IMAP_PORT = int(os.environ.get("MAILSIM_IMAP_PORT", "3143"))
SIM_DOMAIN = "acme-sim.test"


def probe() -> bool:
    """探活走和干活同一条路：真连 SMTP。连不上 = 这轮没有邮件模拟。"""
    try:
        with smtplib.SMTP(HOST, SMTP_PORT, timeout=3) as s:
            s.noop()
        return True
    except Exception:                                         # noqa: BLE001
        return False


def auth_results(from_domain: str, ok: bool = True) -> str:
    """收件服务器视角的 Authentication-Results 头。"""
    r = "pass" if ok else "fail"
    return f"mx.{SIM_DOMAIN}; dmarc={r} header.from={from_domain}"


def send(frm: str, to: str, subject: str, body: str,
         auth_domain: str | None = None, auth_ok: bool = True,
         attachments: dict | None = None, headers: dict | None = None) -> str:
    """发一封信。From 可以随便写 —— 冒充攻击就靠这个模拟。

    auth_domain 给了才带 Authentication-Results（缺失 = 验真必须拒，
    这是 M4 那条规则的第三态）。attachments: {文件名: bytes}，
    给 SaaS 导出路径（readme 8.2 邮件附件）用。
    """
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Date"] = formatdate()
    m["Message-ID"] = make_msgid(domain=SIM_DOMAIN)
    if auth_domain:
        m["Authentication-Results"] = auth_results(auth_domain, auth_ok)
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    for name, data in (attachments or {}).items():
        m.add_attachment(data, maintype="application",
                         subtype="octet-stream", filename=name)
    with smtplib.SMTP(HOST, SMTP_PORT, timeout=10) as s:
        s.send_message(m)
    return m["Message-ID"]


def subject_of(m) -> str:
    """解码 Subject 的 MIME encoded-words —— 中文主题在线路上是
    `=?utf-8?b?..?=`，不解码的话字符串匹配永远落空。"""
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(m["Subject"] or "")))
    except Exception:                                         # noqa: BLE001
        return m["Subject"] or ""


def fetch(mailbox: str, limit: int = 50) -> list:
    """读某个收件人的邮箱，旧 → 新。auth.disabled：密码填什么都行。"""
    out = []
    im = imaplib.IMAP4(HOST, IMAP_PORT, timeout=10)
    try:
        im.login(mailbox, mailbox)
        im.select("INBOX", readonly=True)
        _, data = im.search(None, "ALL")
        for i in (data[0].split() or [])[-limit:]:
            _, msg = im.fetch(i, "(RFC822)")
            # policy=default 才是现代 EmailMessage（有 iter_attachments）；
            # 不给的话是 compat32 的老 Message
            out.append(email.message_from_bytes(
                msg[0][1], policy=email.policy.default))
    finally:
        try:
            im.logout()
        except Exception:                                     # noqa: BLE001
            pass
    return out
