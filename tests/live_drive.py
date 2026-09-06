#!/usr/bin/env python3
"""演练驱动：**扮人**跟真网关打交道。四个动词，一个都不多。

    python3 tests/live_drive.py env                    # 打印该导出的环境变量
    python3 tests/live_drive.py up                     # 起 callback + 网关
    python3 tests/live_drive.py say wang@acme.com "主题" "正文"
    python3 tests/live_drive.py click                  # 从收件箱点批准链接
    python3 tests/live_drive.py choose start_silver    # 点阶段提案的某个选项
    python3 tests/live_drive.py wait                   # 等一轮 cron 唤醒跑完
    python3 tests/live_drive.py tick 3                 # click + wait 跑 3 轮
    python3 tests/live_drive.py inbox boss@acme.com    # 看某人收到了什么
    python3 tests/live_drive.py down                   # 停网关与 callback

在此之前这些是临时敲的命令和 `/tmp/say.py` 之类的散脚本，每次重写一遍，
而每次重写都会漏掉一两条「不知道就会卡住」的规矩（见 docs/live-rehearsal.md）。

**这个脚本只扮人。** 它不替 Agent 调任何工具、不写任何权限表 ——
那是被测对象的活。
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "services"),
                str(ROOT / "plugins")]

HERMES = os.environ.get("HERMES", "/Users/lingyukong/Documents/GitHub/hermes-agent")
LIVE_HOME = ROOT / ".hermes" / "live-home"
GW_LOG = "/tmp/live_gw.log"
CB_LOG = "/tmp/live_cb.log"
CLICKED = "/tmp/live_clicked.txt"
PEOPLE = ["boss@acme.com", "wang@acme.com", "zhou@acme.com", "sun@acme.com",
          "dba@acme.com", "it@acme.com"]

ENV_LINES = f"""\
export HERMES_HOME={LIVE_HOME} HERMES_ENABLE_PROJECT_PLUGINS=1 CLAW_ROOT={ROOT}
export DATASTEWARD_DB=/tmp/live.db
export DATASTEWARD_TOKEN_SECRET=live-secret APPROVAL_PORT=8795
export APPROVAL_BASE_URL=http://127.0.0.1:8795 REQUIRE_DOUBLE_CONFIRM=0
export CLAW_NO_BOOTSTRAP_SOURCES=1
# 出站也走模拟邮箱 —— 漏一个 MAIL_TRANSPORT 信就发到真 Gmail 去了
export NOTIFY_CHANNEL=email MAIL_TRANSPORT=smtp
export SMTP_HOST=127.0.0.1 SMTP_PORT=3025 SMTP_SECURITY=plain
export MAIL_FROM=claw@acme.test
export MAIL_SPONSOR=boss@acme.com MAIL_OWNER=wang@acme.com MAIL_STEWARD=wang@acme.com
# 入站：网关的 email 平台从同一个 GreenMail 收
export EMAIL_ADDRESS=claw@acme.test EMAIL_PASSWORD=claw
export EMAIL_IMAP_HOST=127.0.0.1 EMAIL_IMAP_PORT=3143 EMAIL_IMAP_SECURITY=plain
export EMAIL_SMTP_HOST=127.0.0.1 EMAIL_SMTP_PORT=3025 EMAIL_SMTP_SECURITY=plain
export EMAIL_POLL_INTERVAL=3 EMAIL_HOME_ADDRESS=boss@acme.com
export EMAIL_ALLOWED_USERS={",".join(PEOPLE)}
export CLAW_RESUME_UNIT_SECONDS=60
unset DATASTEWARD_DSN
"""


def _body(m) -> str:
    """取正文。**multipart 要走 walk()** —— `get_content()` 对
    multipart/mixed 直接抛 KeyError，而那个错长得像「收件箱是空的」。"""
    try:
        if m.is_multipart():
            for p in m.walk():
                if p.get_content_type() == "text/plain":
                    return p.get_content()
            return ""
        return m.get_content()
    except Exception:                                         # noqa: BLE001
        return ""


def cmd_env() -> int:
    print(ENV_LINES)
    print("# 用法：live_drive.py env > /tmp/live_env.sh && . /tmp/live_env.sh")
    return 0


def cmd_up() -> int:
    """起 callback + 网关。**网关必须 --replace** —— 旧实例会占着不放，
    而它的报错长得像「起来了」。"""
    subprocess.Popen([sys.executable, str(ROOT / "services" / "approval_callback.py")],
                     stdout=open(CB_LOG, "w"), stderr=subprocess.STDOUT,
                     env={**os.environ})
    subprocess.run([f"{HERMES}/.venv-h/bin/hermes", "gateway", "stop"],
                   capture_output=True, timeout=60)
    subprocess.Popen([f"{HERMES}/.venv-h/bin/hermes", "gateway", "run", "-v",
                      "--replace"], cwd=str(ROOT), env={**os.environ},
                     stdout=open(GW_LOG, "w"), stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 120:
        log = pathlib.Path(GW_LOG).read_text(errors="replace") \
            if pathlib.Path(GW_LOG).exists() else ""
        if "email connected" in log:
            print(f"网关就绪（{int(time.time() - t0)}s）· 日志 {GW_LOG}")
            print("⚠️ **信要在这之后再发** —— adapter 启动时会把存量信标记已见，"
                  "\n   之前到的那些它一封都不会处理。")
            return 0
        if "already running" in log or "Traceback" in log:
            print(f"起不来，看 {GW_LOG}")
            return 1
        time.sleep(3)
    print(f"120s 没等到 email connected，看 {GW_LOG}")
    return 1


def cmd_down() -> int:
    subprocess.run([f"{HERMES}/.venv-h/bin/hermes", "gateway", "stop"],
                   capture_output=True, timeout=60)
    subprocess.run(["pkill", "-f", "hermes gateway run"], capture_output=True)
    subprocess.run(["pkill", "-f", "approval_callback.py"], capture_output=True)
    print("网关与 callback 已停")
    return 0


def cmd_say(frm: str, subject: str, body: str, wait: int = 240) -> int:
    """发一封信，等回信。合法 persona 带 dmarc=pass 的验真头。"""
    import mailsim
    box_before = len(mailsim.fetch(frm))
    dom = frm.split("@", 1)[1]
    mailsim.send(frm, "claw@acme.test", subject, body, auth_domain=dom)
    print(f"→ {frm}: {subject}")
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(5)
        ms = mailsim.fetch(frm)
        if len(ms) > box_before:
            m = ms[-1]
            print(f"\n← Claw（{int(time.time() - t0)}s）: "
                  f"{mailsim.subject_of(m)}\n")
            print(_body(m)[:2000])
            return 0
    print(f"\n（{wait}s 没等到回信 —— 它可能还在干活，"
          f"或者上一轮没跑完就被这封打断了）")
    return 1


def cmd_click() -> int:
    """从**收件箱**点批准链接。只点 approve，点过的不再点。"""
    import mailsim
    seen = set()
    p = pathlib.Path(CLICKED)
    if p.exists():
        seen = set(p.read_text().split())
    n = 0
    for box in PEOPLE:
        for m in mailsim.fetch(box)[-15:]:
            for url in re.findall(r"http://[^\s]+/approve\?t=[\w.\-]+", _body(m)):
                if url in seen:
                    continue
                seen.add(url)
                try:
                    with urllib.request.urlopen(url, timeout=8) as r:
                        if r.status == 200:
                            n += 1
                            print(f"  ✅ {box}: {mailsim.subject_of(m)[:52]}")
                except Exception:                             # noqa: BLE001
                    pass
    p.write_text("\n".join(seen))
    print(f"点了 {n} 个")
    return 0


def cmd_choose(option: str) -> int:
    """点阶段提案里的某个选项（start_silver / push_unfinished / abandon_rest）。"""
    import base64
    import mailsim
    for box in ("boss@acme.com", "wang@acme.com"):
        for m in reversed(mailsim.fetch(box)):
            if "阶段提案" not in mailsim.subject_of(m):
                continue
            for url in re.findall(r"http://[^\s]+/choose\?t=[\w.\-]+", _body(m)):
                tok = url.split("t=", 1)[1].split(".", 1)[0]
                try:
                    d = json.loads(base64.urlsafe_b64decode(
                        tok + "=" * (-len(tok) % 4)))
                except Exception:                             # noqa: BLE001
                    continue
                if d.get("d") != option:
                    continue
                with urllib.request.urlopen(url, timeout=10) as r:
                    print(f"{box} 点了「{option}」→ {r.status}")
                return 0
            break
    print(f"收件箱里没有带 {option} 的阶段提案 —— 周报还没跑过？")
    return 1


def cmd_wait(rounds: int = 1) -> int:
    """等 cron 真的跑完一轮。**认 `Running job` 的计数，不是傻等秒数。**

    每轮结束后多给 50 秒：唤醒会点模型，模型要时间。
    """
    log = pathlib.Path(GW_LOG)
    for i in range(rounds):
        n0 = log.read_text(errors="replace").count("Running job 'data-steward-resume'") \
            if log.exists() else 0
        t0 = time.time()
        while time.time() - t0 < 300:
            time.sleep(8)
            n = log.read_text(errors="replace").count("Running job 'data-steward-resume'") \
                if log.exists() else 0
            if n > n0:
                break
        else:
            print(f"  第 {i + 1} 轮：300s 没等到 cron —— 作业被 drift 跳过了？"
                  f"（换过模型就会，看日志里的 drift_skip）")
            return 1
        time.sleep(50)
        print(f"  第 {i + 1}/{rounds} 轮唤醒跑完")
    return 0


def cmd_tick(rounds: int = 3) -> int:
    """点链接 + 等唤醒，跑若干轮 —— 演练里最常用的动作。"""
    for i in range(rounds):
        cmd_click()
        if cmd_wait(1):
            return 1
    return 0


def cmd_inbox(box: str) -> int:
    import mailsim
    ms = mailsim.fetch(box)
    print(f"\n{box}：{len(ms)} 封\n")
    for i, m in enumerate(ms):
        print(f"  [{i}] {mailsim.subject_of(m)[:64]}")
    if ms:
        print(f"\n--- 最新一封 ---\n{_body(ms[-1])[:1500]}")
    return 0


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        print(__doc__)
        return 0
    cmd, rest = a[0], a[1:]
    if cmd == "env":
        return cmd_env()
    if cmd == "up":
        return cmd_up()
    if cmd == "down":
        return cmd_down()
    if cmd == "say":
        if len(rest) < 3:
            print("用法：say <from> <主题> <正文>")
            return 2
        return cmd_say(rest[0], rest[1], rest[2])
    if cmd == "click":
        return cmd_click()
    if cmd == "choose":
        return cmd_choose(rest[0] if rest else "start_silver")
    if cmd == "wait":
        return cmd_wait(int(rest[0]) if rest else 1)
    if cmd == "tick":
        return cmd_tick(int(rest[0]) if rest else 3)
    if cmd == "inbox":
        return cmd_inbox(rest[0] if rest else "boss@acme.com")
    print(f"未知命令 {cmd!r}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
