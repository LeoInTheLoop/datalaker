"""邮件通道。SMTP（内网/应用专用密码）或 Gmail API（OAuth）。"""
import base64
import sys
from email.message import EmailMessage

from . import E, ROOT, Notifier, approval_links

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

RECEIPT = """<div style="font:15px/1.65 -apple-system,system-ui,sans-serif;max-width:34rem">
<p>你已<b>{verb}</b>：</p>
<table style="border-collapse:collapse;margin:1rem 0;font-size:14px">
<tr><td style="padding:.3rem .9rem .3rem 0;color:#666">动作</td><td><b>{tool}</b></td></tr>
<tr><td style="padding:.3rem .9rem .3rem 0;color:#666">对象</td><td><code>{target}</code></td></tr>
<tr><td style="padding:.3rem .9rem .3rem 0;color:#666">审计号</td><td><code>{aid}</code></td></tr>
</table>
<p style="color:#666;font-size:13px">{note}</p></div>"""


class EmailNotifier(Notifier):
    name = "email"

    def _build(self, to, subject, text, html):
        m = EmailMessage()
        m["To"] = to
        m["From"] = E.get("MAIL_FROM", "") or E.get("SMTP_USER", "")
        m["Subject"] = subject
        m.set_content(text)
        m.add_alternative(html, subtype="html")
        return m

    def send_approval(self, to, approval_id, tool, target, reason, approver):
        ok, no = approval_links(approval_id, approver)
        m = self._build(
            to, f"[数据管家] 请批准：{tool} · {target}",
            f"需要你批准：{tool}（{target}）\n理由：{reason}\n\n"
            f"批准：{ok}\n拒绝：{no}\n\n链接 72 小时有效，只能用一次。",
            HTML.format(tool=tool, target=target, reason=reason, approve=ok, deny=no))
        return self._send(m)

    def send_receipt(self, to, approval_id, decision, tool, target, approver):
        approved = decision == "approve"
        verb = "批准" if approved else "拒绝"
        note = ("该动作将开始执行。若这不是你的本意，请立即回复本邮件。"
                if approved else
                "该动作不会执行，Agent 也不会就同一动作重复打扰你。")
        m = self._build(
            to, f"[数据管家] 已{verb}：{tool} · {target}",
            f"你已{verb}：{tool}（{target}）\n审计号：{approval_id}\n\n{note}",
            RECEIPT.format(verb=verb, tool=tool, target=target,
                           aid=approval_id, note=note))
        return self._send(m)

    def send_notice(self, to, subject, body):
        return self._send(self._build(
            to, subject, body,
            f'<div style="font:15px/1.65 -apple-system,system-ui,sans-serif;'
            f'max-width:34rem;white-space:pre-wrap">{body}</div>'))

    # ---------------- 传输 ----------------
    def _send(self, msg):
        if E.get("MAIL_TRANSPORT", "smtp") == "gmail_api":
            return self._gmail(msg)
        return self._smtp(msg)

    def _smtp(self, msg):
        import smtplib
        user, pw = E.get("SMTP_USER", ""), E.get("SMTP_PASS", "")
        if not (user and pw):
            raise RuntimeError("SMTP_USER / SMTP_PASS 未配置")
        if not msg["From"]:
            del msg["From"]
            msg["From"] = user
        with smtplib.SMTP(E.get("SMTP_HOST", "smtp.gmail.com"),
                          int(E.get("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return {"channel": "email", "transport": "smtp", "to": msg["To"]}

    def _gmail(self, msg):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build as gbuild
        creds = Credentials.from_authorized_user_file(
            str(ROOT / "secrets" / "gmail_token.json"),
            ["https://www.googleapis.com/auth/gmail.send",
             "https://www.googleapis.com/auth/gmail.readonly"])
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = gbuild("gmail", "v1", credentials=creds).users().messages().send(
            userId="me", body={"raw": raw}).execute()
        return {"channel": "email", "transport": "gmail_api",
                "to": msg["To"], "id": r.get("id")}
