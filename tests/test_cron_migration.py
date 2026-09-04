"""定时任务归 Hermes 的 cron（重排 M4：删重复）。

这一组不起 Hermes —— 它验的是**交接面**：作业表、壳脚本、以及
「两边不会同时点火」。真正的点火由 Hermes 自己的 cron 测试保证。
"""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# **按文件路径加载，不要把 claw 目录塞进 sys.path。**
# 这个模块叫 `cron.py`，而 Hermes 自己也有一个顶层 `cron` 包 ——
# 一旦 claw 目录进了 sys.path，`from cron.jobs import ...` 就会撞到
# 我们自己这个模块上，报「cron is not a package」。
# 插件里它只被相对导入（`from .cron import`），所以真实运行时不会撞；
# 测试也得照真实方式加载，否则测出来的是一个假的失败原因。
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "claw_cron", ROOT / ".hermes" / "plugins" / "claw" / "cron.py")
C = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(C)

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== 作业表 ===\n")

chk("三件事都有作业：恢复 / 升级 / 周报", set(C.JOBS) == {
    "claw-resume", "claw-escalate", "claw-weekly-report"}, str(sorted(C.JOBS)))

# 恢复要「等的人回了就往下走」，一小时太慢；升级阶梯按天算，一分钟太密。
chk("恢复是分钟级、升级是小时级（节奏对得上各自的语义）",
    "minute" in C.JOBS["claw-resume"]["schedule"]
    and "hour" in C.JOBS["claw-escalate"]["schedule"],
    f'{C.JOBS["claw-resume"]["schedule"]} / {C.JOBS["claw-escalate"]["schedule"]}')

print("\n=== 该不该点模型 ===\n")

# 催办和周报是确定性脚本 —— 走 no_agent 就不会每小时点一次模型。
for n in ("claw-escalate", "claw-weekly-report"):
    chk(f"{n} 不点模型（确定性脚本）", C.JOBS[n].get("no_agent") is True)

# 恢复必须让 Agent 自己再调一次工具（M5 驱动反转），所以要点模型。
# 但「每分钟点一次模型」显然不行 —— 靠 monitor_script 把它压回一次 SQL。
_res = C.JOBS["claw-resume"]
chk("恢复要点模型（Agent 自己再调工具，不是脚本代劳）",
    not _res.get("no_agent") and bool(_res.get("prompt")))
chk("**但靠 monitor 压住**：输出没变就整个跳过，不点模型",
    bool(_res.get("monitor_script")), str(_res.get("monitor_script")))
chk("恢复作业没有 script（monitor 的输出就是它的输入）",
    not _res.get("script"))

# monitor 源的输出必须按字节稳定 —— 带一个时间戳就等于每分钟点一次模型。
_mon = (ROOT / "ops" / "resumable.py").read_text(encoding="utf-8")
chk("monitor 源不打时间戳（否则哈希每次都变，等于没有 monitor）",
    "time.time" not in _mon and "strftime" not in _mon
    and "datetime" not in _mon)
chk("monitor 源排过序（谁先批的顺序会抖，白白唤醒模型）",
    "sorted(" in _mon)

import subprocess as _sp2
_r2 = _sp2.run([sys.executable, str(ROOT / "ops" / "resumable.py")],
               capture_output=True, text=True, timeout=60)
_r3 = _sp2.run([sys.executable, str(ROOT / "ops" / "resumable.py")],
               capture_output=True, text=True, timeout=60)
chk("连跑两次输出逐字节相同（这就是 monitor 生效的前提）",
    _r2.returncode == 0 and _r2.stdout == _r3.stdout,
    f"rc={_r2.returncode} len={len(_r2.stdout)}")

print("\n=== 壳脚本必须是真文件 ===\n")

# Hermes 会 resolve() 之后校验路径是否仍在 scripts 目录内，
# 软链会被穿透后拒掉 —— 放软链的话作业每次点火都失败，
# 而 Hermes 那边只是记一条错误，我们这边看不出来。
HOME = ROOT / ".hermes" / "home"
missing = C.scripts_present(str(HOME))
chk("每个作业用到的脚本都在 HERMES_HOME/scripts/ 下", not missing, str(missing))
_ALL = [x for job in C.JOBS.values() for x in C.scripts_of(job)]
for s in _ALL:
    f = HOME / "scripts" / s
    chk(f"{s} 不是软链（软链会被 resolve 穿透后拒掉）",
        f.exists() and not f.is_symlink())

print("\n=== 壳里不放逻辑 ===\n")

for s in _ALL:
    src = (HOME / "scripts" / s).read_text(encoding="utf-8")
    # 壳一旦开始判断，就有了第二处需要维护的逻辑，
    # 而它在 HERMES_HOME 里、不在 ops/ 里，改的人根本想不到去看。
    chk(f"{s} 只是转发（没有 if/while）",
        "\nif " not in src and "while " not in src)

_init = (ROOT / ".hermes" / "plugins" / "claw"
         / "__init__.py").read_text(encoding="utf-8")
chk("插件里用的是相对导入（`from .cron`）—— 绝对导入会撞上 Hermes 的 cron 包",
    "from .cron import" in _init)

print("\n=== 不会双份点火 ===\n")

r = subprocess.run([sys.executable, str(ROOT / "ops" / "scheduler.py")],
                   capture_output=True, text=True, timeout=30,
                   env={**os.environ, "CLAW_STANDALONE_SCHEDULER": "0"})
chk("旧的独立 scheduler 默认不再守护（否则催办邮件发两遍）",
    r.returncode == 0 and "Hermes" in r.stdout, r.stdout.splitlines()[0][:50]
    if r.stdout else r.stderr[:50])
chk("但告诉了怎么手动跑一次", "--once" in r.stdout)

print("\n=== 拿不到 Hermes 时不崩 ===\n")

# 我们自己的测试进程里没有 `cron.jobs`（那是 Hermes 的）。
# ensure_jobs 必须安静地报「跳过」，而不是抛出去把插件注册带崩 ——
# 门禁比定时催办重要得多。
res = C.ensure_jobs(str(ROOT))
chk("不在 Hermes 进程里时安静跳过", res.get("skipped") is not None,
    str(res.get("skipped"))[:50])
chk("跳过的原因是「找不到 Hermes 的 cron」，不是名字撞车",
    "not a package" not in str(res.get("skipped")),
    str(res.get("skipped"))[:60])
chk("跳过时也没有误报「已创建」", not res.get("created"))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
