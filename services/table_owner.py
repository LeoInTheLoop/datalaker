#!/usr/bin/env python3
"""场景里的表负责人：收 steward 的信，凭人设作答，回信。

**他们不是 steward 的下属，也不在治理边界里。** 没有治理工具、没有库连接、
没有门禁 —— 就是两个会回邮件的人。所以这里不需要 Hermes：一个 Hermes
profile 是一整个常驻网关，而这两位要做的事只有「读信、想、回信」。

**他们会说错话，而且是故意的**（`demo/table_owners.json` 的 `mistakes`）。
考的是 steward 会不会拿真实数据去核对人的说法 —— 一条错得很硬（字段不存在，
一查就崩），一条错得很软（能算出数字，但数字是错的）。软的那条才是考点。

    python3 services/table_owner.py            # 常驻，每 5 秒收一次信
    python3 services/table_owner.py --once     # 收一轮就退出
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import json
import os
import pathlib
import smtplib
import sys
import time
import urllib.request
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNERS_FILE = pathlib.Path(os.environ.get("DEMO_OWNERS_FILE", ROOT / "demo" / "table_owners.json"))
HOST = os.environ.get("MAILSIM_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAILSIM_SMTP_PORT", "13025"))
IMAP_PORT = int(os.environ.get("MAILSIM_IMAP_PORT", "13143"))


def env_file() -> dict:
    out = {}
    path = ROOT / ".env"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key] = value.strip().strip('"').strip("'")
    return out


ENV = env_file()


def cfg(key: str) -> str:
    return os.environ.get(key) or ENV.get(key, "")


def prompt_of(owner: dict) -> str:
    """人设 → system prompt。**错误写成「你就是这么记的」，不是「你要撒谎」**
    —— 说成撒谎，模型会演得很假，一被追问就立刻招供，测不出真实的固执。"""
    return "\n".join([
        owner["persona"],
        "",
        f"你管的表：{owner['table']}",
        "",
        "你记得的事（这些是对的）：",
        *[f"- {x}" for x in owner.get("knows", [])],
        "",
        "你也记得下面这些，你完全相信它们是对的：",
        *[f"- {x}" for x in owner.get("mistakes", [])],
        "",
        "你不知道的事（被问到就直说不知道，别猜）：",
        *[f"- {x}" for x in owner.get("does_not_know", [])],
        "",
        "回信要求：中文，像真人回工作邮件，三五句话，不用敬语套话。"
        "不要写 SQL。不要说自己是 AI 或在扮演。只回正文，不要写主题行。",
    ])


def models() -> list[str]:
    """主模型 + 降级链。**免费额度耗尽是 403 且不可重试** —— 只配一个就是单点，
    而额度什么时候空掉不写在任何配置里，只有打过去才知道（实测撞到过）。"""
    chain = [cfg("OPENAI_MODEL")]
    chain += [m.strip() for m in cfg("OPENAI_MODEL_FALLBACKS").split(",") if m.strip()]
    return list(dict.fromkeys([m for m in chain if m]))


def answer(owner: dict, subject: str, body: str) -> str:
    last = ""
    for model in models():
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt_of(owner)},
                {"role": "user", "content": f"你收到一封邮件。\n主题：{subject}\n\n{body}"},
            ],
            "max_tokens": 700,
        }
        request = urllib.request.Request(
            cfg("OPENAI_BASE_URL").rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + cfg("OPENAI_API_KEY"),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read())
            text = (data.get("choices") or [{}])[0].get("message", {}).get(
                "content", "").strip()
            if text:
                return text
        except Exception as exc:                          # noqa: BLE001
            last = f"{model}: {type(exc).__name__}"
            print(f"    模型 {model} 不可用（{type(exc).__name__}），换下一个")
    raise RuntimeError("没有可用模型 " + last)


def send(sender: str, to: str, subject: str, body: str, in_reply_to: str = "") -> None:
    message = EmailMessage()
    message["From"], message["To"] = sender, to
    message["Subject"] = subject
    message["Message-ID"] = email.utils.make_msgid(domain="acme-sim.test")
    if in_reply_to:
        message["In-Reply-To"] = message["References"] = in_reply_to
    message.set_content(body)
    with smtplib.SMTP(HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.send_message(message)


def poll(owner: dict) -> int:
    """收一轮未读信并回复。**用 UNSEEN 而不是自己记 uid** —— steward 那边
    重置邮箱是常事，本地游标一旦和邮箱对不上，要么漏回要么重复回。"""
    handled = 0
    box = owner["address"]
    imap = imaplib.IMAP4(HOST, IMAP_PORT, timeout=10)
    try:
        imap.login(box, box)
        imap.select("INBOX")
        _, ids = imap.search(None, "UNSEEN")
        for uid in (ids[0].split() if ids and ids[0] else []):
            # **先 PEEK，回信成功之后再标已读。** 反过来的话，模型调用一失败
            # 这封信就被吃掉了 —— 不会重试，对方永远等不到回音，而错误还在
            # 缓冲区里看不见（实测：主模型额度耗尽，两位负责人集体失联）。
            _, raw = imap.fetch(uid, "(BODY.PEEK[])")
            message = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            sender = email.utils.parseaddr(message.get("From", ""))[1]
            subject = str(message.get("Subject") or "")
            try:
                body = message.get_body(preferencelist=("plain",)).get_content()
            except Exception:                             # noqa: BLE001
                body = ""
            if not sender or sender == box:
                continue
            reply = answer(owner, subject, body)
            if not reply:
                continue
            send(box, sender,
                 subject if subject.lower().startswith("re:") else f"Re: {subject}",
                 reply, message.get("Message-ID", ""))
            imap.store(uid, "+FLAGS", "\\Seen")          # 回出去了才算处理过
            print(f"[{owner['name']} · {box}] ← {sender} | {subject[:40]}")
            print(f"    → {reply[:160]}")
            handled += 1
    finally:
        try:
            imap.logout()
        except Exception:                                 # noqa: BLE001
            pass
    return handled


def main() -> None:
    owners = json.loads(OWNERS_FILE.read_text(encoding="utf-8"))["owners"]
    once = "--once" in sys.argv
    print(f"表负责人上线：{', '.join(o['address'] for o in owners)}"
          f"  (mail {HOST}:{SMTP_PORT}/{IMAP_PORT}, model {cfg('OPENAI_MODEL')})")
    while True:
        for owner in owners:
            try:
                poll(owner)
            except Exception as exc:                      # noqa: BLE001
                print(f"[{owner['address']}] {type(exc).__name__}: {str(exc)[:120]}")
        if once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
