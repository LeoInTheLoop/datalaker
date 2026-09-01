#!/usr/bin/env python3
"""定时驱动器（readme 5.4 / 5.5 / 20.5）。

**独立于 Agent 运行**：Agent 挂了，催办和周报也不能停。
这正是场景 3 缺的那个计时器——脚本本身不会自己跑。

    每 1 小时    超时逐级升级 → 优雅放弃
    每周一 09:00 周报（无进展也发）

ponytail: 用一个循环 + 状态文件，不引入 APScheduler/Celery。
容器重启后从状态文件恢复，不会重复发周报。
"""
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = pathlib.Path(os.environ.get("SCHEDULER_STATE", "/tmp/claw-scheduler.json"))
ESCALATE_EVERY = int(os.environ.get("ESCALATE_INTERVAL_SEC", "3600"))
REPORT_DOW = int(os.environ.get("REPORT_DAY_OF_WEEK", "0"))    # 0=周一
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "9"))


def load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"last_escalate": 0, "last_report_day": ""}


def save(st):
    try:
        STATE.write_text(json.dumps(st))
    except Exception:
        pass


def run(script):
    t0 = time.time()
    r = subprocess.run([sys.executable, str(ROOT / "ops" / script)],
                       capture_output=True, text=True, timeout=300)
    tag = "ok" if r.returncode == 0 else f"rc={r.returncode}"
    out = (r.stdout or r.stderr).strip().replace("\n", " | ")[:160]
    print(f"[{time.strftime('%H:%M:%S')}] {script} {tag} ({time.time()-t0:.1f}s) {out}",
          flush=True)


def main():
    print(f"scheduler 启动：升级每 {ESCALATE_EVERY}s，"
          f"周报每周 {REPORT_DOW} 的 {REPORT_HOUR}:00", flush=True)
    while True:
        st = load()
        now = time.time()
        lt = time.localtime(now)

        if now - st["last_escalate"] >= ESCALATE_EVERY:
            run("escalate.py")
            st["last_escalate"] = now

        today = time.strftime("%Y-%m-%d", lt)
        if (lt.tm_wday == REPORT_DOW and lt.tm_hour >= REPORT_HOUR
                and st["last_report_day"] != today):
            run("weekly-report.py")
            st["last_report_day"] = today

        save(st)
        time.sleep(60)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run("escalate.py")
        run("weekly-report.py")
    else:
        main()
