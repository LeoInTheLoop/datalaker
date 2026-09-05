"""把定时循环交给 Hermes 的 cron（重排 M4：删重复）。

原来是 `ops/scheduler.py` 一个 89 行的 `while True` + 状态文件，跑在
compose 的 `scheduler` 服务里。Hermes 的 `cron/` 已经把同一件事做完了，
而且做得更全：作业锁（同一作业不会并发起两份）、一次性 claim fence、
重启后按 `next_run` 恢复而不是靠状态文件、失败投递、输出留存。

`no_agent=True` 用在**催办**上 —— 升级阶梯是确定性判断，不需要模型，
走 no_agent 就不会每小时点一次模型。

**周报是反例，别照抄**：它要写给人看、要给建议和理由，所以要点模型。
一周一次，成本可忽略。它的四段数字仍由脚本查库算（那是事实，不该编），
脚本因此从「作业本体」降级成**数据源** —— agent 模式下 stdout 会以
`## Script Output` 注入 prompt。文体与「该调哪个工具」由 skill 带进去。

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
#   no_agent  催办     —— 确定性脚本，走 no_agent 就不会每小时点一次模型
#   script+skill 周报  —— 脚本供事实、skill 供文体，模型只负责写提案
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
            "如果某一条又被拦下，那说明它还需要新的审批，跳过它继续下一条。\n"
            "标了 SILVER 的那些不是等人的线，是**清洗轮已经开了、这张表还没洗**："
            "先 `propose_cleaning` 看提案，再用提案里给出的规则名"
            "（原样抄，别改写）调 `apply_cleaning_rule`。"
        ),
    },
    "claw-escalate": {
        "schedule": "every 1 hour",
        "script": "claw_escalate.py",
        "no_agent": True,
    },
    # 周报**要点模型**：它写给人看，要给建议和理由。一周一次，成本可忽略。
    # 但四段固定数字（完成 / 卡在谁那里 / 需要决策 / 开销）仍由脚本查库算 ——
    # 那些是事实，不该让模型编。脚本从「作业本体」降级成**数据源**：
    # agent 模式下它的 stdout 会以 `## Script Output` 注入 prompt。
    # skill 只管文体和「必须调 propose_stage_decision」；能不能开下一轮
    # 由门禁的 `_silver_round_closed` 管（引导走 prompt，强制走门禁）。
    "claw-weekly-report": {
        "schedule": "every monday at 09:00",
        "script": "claw_weekly_report.py",
        "skills": ["claw-stage-proposal"],
        "prompt": (
            "上面 Script Output 里是这一周的四段事实，已经查过库了 —— "
            "**不要重算，也不要改写数字**。\n"
            "按 claw-stage-proposal 这个 skill 写：压成一段给人看的小结，"
            "然后调 `propose_stage_decision` 把它变成三选一的提案，"
            "带上你的建议和理由。\n"
            "调完就停下来。开不开下一轮不由你决定。"
        ),
    },
}


def _current_model(home: str) -> tuple:
    """Hermes 当前配置的模型与 provider。读不到就 (None, None)。

    **为什么要读它**：Hermes 对没有 pin 模型的 cron 作业有花费保护 ——
    全局模型一换，作业就被 `[drift_skip:silent]` **静默跳过**，
    而且「告警只发一次，之后一直跳过」。表现是恢复作业彻底不动，
    看起来像 monitor 坏了。实测踩过：换掉一个欠费的模型之后，
    整条恢复链停摆，日志里只有一行 ERROR，再也没有第二行。

    所以作业创建时就 pin 到当前配置，并把 model 纳入 drift 比对：
    换模型时 `ensure_jobs` 按表把它改过来，而不是等 Hermes 去跳过。
    """
    # **不用 yaml**：这个模块要保持 import 轻量（插件在发现阶段就被导入），
    # 而且跑测试的解释器不一定装了 PyYAML —— 实测系统 python3 就没有，
    # 表现是「读不到模型」于是 drift 检测整条失效，静默得很。
    # 配置格式是固定的，正则读 `model:` 段下那两行足够。
    import os as _os
    import re as _re
    try:
        txt = open(_os.path.join(home, "config.yaml"), encoding="utf-8").read()
    except Exception:                                        # noqa: BLE001
        return (None, None)
    m = _re.search(r"^model:\s*$(.*?)(?=^\S|\Z)", txt, _re.M | _re.S)
    body = m.group(1) if m else ""
    d = _re.search(r'^\s+default:\s*"?([^"\n]+)"?\s*$', body, _re.M)
    pv = _re.search(r'^\s+provider:\s*"?([^"\n]+)"?\s*$', body, _re.M)
    return (d.group(1).strip() if d else None,
            pv.group(1).strip() if pv else None)


def scripts_of(job: dict) -> list:
    """一个作业用到的所有脚本（作业本体 + monitor 源）。"""
    return [job[k] for k in ("script", "monitor_script") if job.get(k)]


def _drift(rec: dict, job: dict, parse_schedule) -> list:
    """存量作业与定义表对不上的字段名。空列表 = 一致。

    **每加一个会写进 cron 的字段，这里必须同步加一行比对。**
    漏一个的后果是「名字对得上、定义对不上」—— 存量作业带着旧定义
    一直跑，而失败只记在 Hermes 侧的日志里，我们这边什么都看不见。
    `claw-resume` 从 no_agent 脚本改成 monitor 作业时踩过一次；
    `skills` 是第二个这样的字段（周报带 skill 之后）。
    """
    diff = [k for k in ("script", "monitor_script")
            if (rec.get(k) or None) != (job.get(k) or None)]
    if bool(rec.get("no_agent")) != bool(job.get("no_agent")):
        diff.append("no_agent")
    if (rec.get("prompt") or "") != (job.get("prompt") or ""):
        diff.append("prompt")
    # skills 是列表：**顺序无关，集合相等才算一致**。
    # 按列表比会因为顺序抖动而每次都判 drift，于是每次注册都 update 一遍。
    if set(rec.get("skills") or []) != set(job.get("skills") or []):
        diff.append("skills")
    # 模型：作业上 pin 的必须与当前全局配置一致，否则 Hermes 的花费保护
    # 会把它**静默跳过**（`[drift_skip:silent]`，告警只发一次）。
    want_model = job.get("_model")
    if want_model and (rec.get("model") or None) != want_model:
        diff.append("model")
    want = parse_schedule(job["schedule"])
    have = rec.get("schedule") or {}
    if {k: have.get(k) for k in want} != want:
        diff.append("schedule")
    return diff


def ensure_jobs(root: str) -> dict:
    """幂等地把三个作业登记进 Hermes cron。

    按 **name** 判重 —— Hermes 每次启动都会调 `register`，
    不判重的话每重启一次就多三个同样的作业。

    但名字相同不代表定义相同：`claw-resume` 就从 no_agent 脚本改成过
    monitor 作业。只按名字跳过的话，老作业会一直跑一个已删掉的脚本，
    而失败只记在 Hermes 侧的日志里，我们这边什么都看不见。
    所以存量作业要与定义表**逐字段比对**，对不上就按表修正并喊出来。
    """
    out = {"created": [], "updated": [], "existing": [], "skipped": None}
    try:
        from cron.jobs import (create_job, list_jobs, parse_schedule,
                               update_job)
    except Exception as e:                                    # noqa: BLE001
        # 不在 Hermes 进程里（比如我们自己的测试直接 import 这个模块），
        # 或者 croniter 没装。**不能让插件注册因此崩掉。**
        out["skipped"] = f"{type(e).__name__}: {e}"
        return out

    try:
        have = {j.get("name"): j for j in list_jobs(include_disabled=True)}
    except Exception as e:                                    # noqa: BLE001
        out["skipped"] = f"list_jobs: {e}"
        return out

    model, provider = _current_model(os.environ.get("HERMES_HOME", ""))
    for name, job in JOBS.items():
        job = dict(job, _model=model)      # pin 到当前配置，见 _current_model
        rec = have.get(name)
        if rec is not None:
            try:
                diff = _drift(rec, job, parse_schedule)
                if not diff:
                    out["existing"].append(name)
                    continue
                update_job(rec["id"], {
                    "schedule": job["schedule"],
                    "prompt": job.get("prompt") or "",
                    "script": job.get("script"),
                    "monitor_script": job.get("monitor_script"),
                    "no_agent": bool(job.get("no_agent")),
                    "skills": list(job.get("skills") or []),
                    **({"model": model, "provider": provider} if model else {}),
                })
                out["updated"].append(f"{name}({', '.join(diff)})")
            except Exception as e:                            # noqa: BLE001
                out.setdefault("errors", []).append(f"{name}: {e}")
            continue
        try:
            create_job(prompt=job.get("prompt"), schedule=job["schedule"],
                       name=name, script=job.get("script"),
                       monitor_script=job.get("monitor_script"),
                       no_agent=bool(job.get("no_agent")),
                       skills=list(job.get("skills") or []) or None,
                       model=model, provider=provider,
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
