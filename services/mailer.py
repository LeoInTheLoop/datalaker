"""审批邮件发送。

一封邮件只问一个决策（readme 5.5）——邮件里只有一个动作、两个按钮。
"""
import base64
import os
import pathlib
import sys
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))

import tokens


def env(path=ROOT / ".env"):
    d = {}
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


E = env()
BASE = E.get("APPROVAL_BASE_URL", "http://127.0.0.1:8787")

HTML = """<div style="font:15px/1.65 -apple-system,system-ui,sans-serif;color:#1a1a1a;
max-width:34rem">
<p>你好，</p>
<p>我是数据管家 Claw。为推进数据资产整理，需要你就<b>一件事</b>做决定：</p>
<table style="border-collapse:collapse;margin:1.2rem 0;font-size:14px">
<tr><td style="padding:.35rem .9rem .35rem 0;color:#666">动作</td><td><b>{tool}</b></td></tr>
<tr><td style="padding:.35rem .9rem .35rem 0;color:#666">对象</td><td><code>{target}</code></td></tr>
<tr><td style="padding:.35rem .9rem .35rem 0;color:#666">理由</td><td>{reason}</td></tr>
</table>
<p>
<a href="{approve}" style="display:inline-block;padding:.55rem 1.4rem;background:#166534;
color:#fff;text-decoration:none;border-radius:.35rem;font-weight:600">批准</a>
&nbsp;&nbsp;
<a href="{deny}" style="display:inline-block;padding:.55rem 1.4rem;background:#fff;
color:#b42318;text-decoration:none;border:1px solid #f3c6c2;border-radius:.35rem;
font-weight:600">拒绝</a>
</p>
<p style="color:#666;font-size:13px;margin-top:1.6rem">
链接 72 小时内有效，只能点击一次。<br>
在你做出决定之前，这个动作不会执行——这不是承诺，是系统层面的拦截。<br>
如果三天内没有回应，我会向上一级汇报，并继续推进其他不受阻塞的工作。
</p></div>"""


def build_message(to, approval_id, tool, target, reason, approver):
    t_ok = tokens.issue(approval_id, "approve", approver)
    t_no = tokens.issue(approval_id, "deny", approver)
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = E.get("MAIL_FROM", "")
    msg["Subject"] = f"[数据管家] 请批准：{tool} · {target}"
    msg.set_content(
        f"需要你批准：{tool}（{target}）\n理由：{reason}\n\n"
        f"批准：{BASE}/approve?t={t_ok}\n拒绝：{BASE}/deny?t={t_no}\n\n"
        f"链接 72 小时有效，只能用一次。")
    msg.add_alternative(HTML.format(
        tool=tool, target=target, reason=reason,
        approve=f"{BASE}/approve?t={t_ok}", deny=f"{BASE}/deny?t={t_no}"), subtype="html")
    return msg


def send(to, approval_id, tool, target, reason, approver):
    """发送审批邮件。

    两种传输方式，由 .env 的 MAIL_TRANSPORT 选择：

    smtp（默认）  应用专用密码 + smtplib。零依赖，不需要 Google 应用验证。
    gmail_api     OAuth。需要在 Google Cloud Console 把自己加进 Test users，
                  否则会报 "has not completed the Google verification process"。

    未配置或发送失败时抛异常 —— 调用方必须当作「通知未送达」处理，
    但动作本身依然被拦截（门禁与通知是两回事）。
    """
    msg = build_message(to, approval_id, tool, target, reason, approver)
    if E.get("MAIL_TRANSPORT", "smtp") == "gmail_api":
        return _send_gmail_api(msg)
    return _send_smtp(msg)


def _send_smtp(msg):
    """SMTP + 应用专用密码。ponytail: smtplib 是标准库，不引入任何依赖。"""
    import smtplib

    host = E.get("SMTP_HOST", "smtp.gmail.com")
    port = int(E.get("SMTP_PORT", "587"))
    user = E.get("SMTP_USER", "")
    pw = E.get("SMTP_PASS", "")
    if not (user and pw):
        raise RuntimeError("SMTP_USER / SMTP_PASS 未配置")

    if not msg["From"]:
        del msg["From"]
        msg["From"] = user

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return {"transport": "smtp", "to": msg["To"]}


def _send_gmail_api(msg):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as gbuild

    creds = Credentials.from_authorized_user_file(
        str(ROOT / "secrets" / "gmail_token.json"),
        ["https://www.googleapis.com/auth/gmail.send",
         "https://www.googleapis.com/auth/gmail.readonly"])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return gbuild("gmail", "v1", credentials=creds).users().messages().send(
        userId="me", body={"raw": raw}).execute()
