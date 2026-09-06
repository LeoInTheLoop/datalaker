#!/usr/bin/env python3
"""定时驱动器（readme 5.4 / 5.5 / 20.5）。**已被 Hermes 的 cron 取代。**

    默认入口是 `.hermes/plugins/data-steward/cron.py` 登记的三个 no_agent 作业。
    这里保留 `--once`（手动兜一次、CI 里跑一遍）；守护模式要显式
    `CLAW_STANDALONE_SCHEDULER=1` 才起，**免得和 Hermes 的 cron 双份点火**。

原来这段注释写的是「**独立于 Agent 运行**：Agent 挂了，催办和周报也不能停」。
搬进 Hermes 之后这条不再成立，**是自觉的取舍**：补回来的方式是 M6 的
体外监控层把 Hermes 拉起来，而不是在体内再养一个独立循环。

    每 1 分钟    恢复「等的人已经回了」的任务
    每 1 小时    超时逐级升级 → 优雅放弃
    每周一 09:00 周报（无进展也发）

恢复放在这里而不是 callback 里，是因为**审批落库与任务往下走是两件事**：
点击的人只负责给出决定，任务什么时候被拉起由调度决定。
这样一条线崩了不会阻塞点击响应，也不需要 callback 认识任何 Pipeline。

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
RESUME_EVERY = int(os.environ.get("RESUME_INTERVAL_SEC", "60"))
REPORT_DOW = int(os.environ.get("REPORT_DAY_OF_WEEK", "0"))    # 0=周一
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "9"))


def load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"last_escalate": 0, "last_report_day": "", "last_resume": 0}


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
    print(f"scheduler 启动：恢复每 {RESUME_EVERY}s，升级每 {ESCALATE_EVERY}s，"
          f"周报每周 {REPORT_DOW} 的 {REPORT_HOUR}:00", flush=True)
    while True:
        st = load()
        now = time.time()
        lt = time.localtime(now)

        if now - st.get("last_resume", 0) >= RESUME_EVERY:
            run("resume.py")
            st["last_resume"] = now

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
    if "--once" not in sys.argv and os.environ.get(
            "CLAW_STANDALONE_SCHEDULER") != "1":
        print("定时任务已交给 Hermes 的 cron（.hermes/plugins/data-steward/cron.py）。\n"
              "  手动跑一次：python3 ops/scheduler.py --once\n"
              "  仍要独立守护：CLAW_STANDALONE_SCHEDULER=1 python3 ops/scheduler.py\n"
              "  两边同时开会双份点火 —— 催办邮件会发两遍。")
        sys.exit(0)
    if "--once" in sys.argv:
        run("resume.py")
        run("escalate.py")
        run("weekly-report.py")
    else:
        main()
