"""把定时循环交给 Hermes 的 cron（重排 M4：删重复）。

原来是 `ops/scheduler.py` 一个 89 行的 `while True` + 状态文件，跑在
compose 的 `scheduler` 服务里。Hermes 的 `cron/` 已经把同一件事做完了，
而且做得更全：作业锁（同一作业不会并发起两份）、一次性 claim fence、
重启后按 `next_run` 恢复而不是靠状态文件、失败投递、输出留存。

`no_agent=True` 是这里的关键 —— 催办和周报不需要模型推理，
它们是确定性脚本。走 no_agent 就不会每小时点一次模型。

## 丢掉了什么，怎么补回来

旧 scheduler 的注释写着「**独立于 Agent 运行**：Agent 挂了，催办和周报
也不能停」。搬进 Hermes 之后这条不再成立 —— Hermes 挂了，cron 也停。
**这是自觉的取舍，不是疏漏**：补回来的方式是 M6 的体外监控层负责把
Hermes 拉起来，而不是在体内再养一个独立循环。在 M6 落地之前，
`ops/scheduler.py --once` 仍然可以手动兜一次。
"""
import os

# name -> (schedule, script)。**表驱动**：加一条定时任务 = 加一行。
JOBS = {
    "claw-resume": ("every 1 minute", "claw_resume.py"),
    "claw-escalate": ("every 1 hour", "claw_escalate.py"),
    "claw-weekly-report": ("every monday at 09:00", "claw_weekly_report.py"),
}


def ensure_jobs(root: str) -> dict:
    """幂等地把三个作业登记进 Hermes cron。

    按 **name** 判重 —— Hermes 每次启动都会调 `register`，
    不判重的话每重启一次就多三个同样的作业。
    """
    out = {"created": [], "existing": [], "skipped": None}
    try:
        from cron.jobs import create_job, list_jobs
    except Exception as e:                                    # noqa: BLE001
        # 不在 Hermes 进程里（比如我们自己的测试直接 import 这个模块），
        # 或者 croniter 没装。**不能让插件注册因此崩掉。**
        out["skipped"] = f"{type(e).__name__}: {e}"
        return out

    try:
        have = {j.get("name") for j in list_jobs(include_disabled=True)}
    except Exception as e:                                    # noqa: BLE001
        out["skipped"] = f"list_jobs: {e}"
        return out

    for name, (schedule, script) in JOBS.items():
        if name in have:
            out["existing"].append(name)
            continue
        try:
            create_job(prompt=None, schedule=schedule, name=name,
                       script=script, no_agent=True, deliver="local",
                       workdir=root)
            out["created"].append(name)
        except Exception as e:                                # noqa: BLE001
            out.setdefault("errors", []).append(f"{name}: {e}")
    return out


def scripts_present(home: str) -> list:
    """三个壳脚本必须真实存在于 HERMES_HOME/scripts/ 下。

    Hermes 会 `resolve()` 之后校验路径是否还在 scripts 目录里，
    **软链会被穿透后拒掉** —— 所以只能放真文件。
    """
    d = os.path.join(home, "scripts")
    return [s for _, s in JOBS.values()
            if not os.path.isfile(os.path.join(d, s))]
