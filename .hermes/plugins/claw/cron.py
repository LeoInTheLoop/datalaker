"""把定时循环交给 Hermes 的 cron（重排 M4：删重复）。

原来是 `ops/scheduler.py` 一个 89 行的 `while True` + 状态文件，跑在
compose 的 `scheduler` 服务里。Hermes 的 `cron/` 已经把同一件事做完了，
而且做得更全：作业锁（同一作业不会并发起两份）、一次性 claim fence、
重启后按 `next_run` 恢复而不是靠状态文件、失败投递、输出留存。

`no_agent=True` 是这里的关键 —— 催办和周报不需要模型推理，
它们是确定性脚本。走 no_agent 就不会每小时点一次模型。

**恢复是例外**：它必须让 Agent 自己再调一次工具（M5 驱动反转），
所以要点模型。但「每分钟点一次模型」显然不行。用 `monitor_script`：
每 tick 先跑一个便宜脚本（`ops/resumable.py`，一次 SQL），按字节哈希输出，
**没变就整个跳过这次 agent 运行**。于是「一分钟一次」的实际成本是一次查询，
只有真有审批落地时才唤醒模型。

## 丢掉了什么，怎么补回来

旧 scheduler 的注释写着「**独立于 Agent 运行**：Agent 挂了，催办和周报
也不能停」。搬进 Hermes 之后这条不再成立 —— Hermes 挂了，cron 也停。
**这是自觉的取舍，不是疏漏**：补回来的方式是 M6 的体外监控层负责把
Hermes 拉起来，而不是在体内再养一个独立循环。在 M6 落地之前，
`ops/scheduler.py --once` 仍然可以手动兜一次。
"""
import os

# name -> 作业定义。**表驱动**：加一条定时任务 = 加一行。
#
# 两种形态，区别只有一个问题：**这件事需不需要模型想一下。**
#
#   no_agent  催办、周报 —— 确定性脚本，走 no_agent 就不会每小时点一次模型
#   monitor   恢复      —— 需要 Agent 自己再调一次工具，所以必须点模型；
#                          但绝不能每分钟点一次。`monitor_script` 每 tick
#                          先跑那个便宜脚本、按字节哈希输出，**没变就整个
#                          跳过这次 agent 运行**（记一次 no_change）。
#                          于是「一分钟一次」的实际成本是一次 SQL 查询。
JOBS = {
    "claw-resume": {
        "schedule": "every 1 minute",
        "monitor_script": "claw_resumable.py",
        "prompt": (
            "有任务线的审批已经有决定了 —— 上面 MONITOR CHANGE DETECTED 里"
            "列出的就是。\n"
            "逐条把它们往下推：对每一行的 run_id，重新执行它当初被拦下的那个动作"
            "（第二列是工具名，第三列是对象）。**票据是一次性的**，"
            "所以直接调用即可，不要再问一遍人。\n"
            "标了 WIP 的那些是被别人的待办挤回来的，不是等谁拍板 —— "
            "同样直接重试。\n"
            "如果某一条又被拦下，那说明它还需要新的审批，跳过它继续下一条。"
        ),
    },
    "claw-escalate": {
        "schedule": "every 1 hour",
        "script": "claw_escalate.py",
        "no_agent": True,
    },
    "claw-weekly-report": {
        "schedule": "every monday at 09:00",
        "script": "claw_weekly_report.py",
        "no_agent": True,
    },
}


def scripts_of(job: dict) -> list:
    """一个作业用到的所有脚本（作业本体 + monitor 源）。"""
    return [job[k] for k in ("script", "monitor_script") if job.get(k)]


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

    for name, job in JOBS.items():
        if name in have:
            out["existing"].append(name)
            continue
        try:
            create_job(prompt=job.get("prompt"), schedule=job["schedule"],
                       name=name, script=job.get("script"),
                       monitor_script=job.get("monitor_script"),
                       no_agent=bool(job.get("no_agent")),
                       deliver="local", workdir=root)
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
    return [s for job in JOBS.values() for s in scripts_of(job)
            if not os.path.isfile(os.path.join(d, s))]
