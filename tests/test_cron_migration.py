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
    "claw_cron", ROOT / ".hermes" / "plugins" / "data-steward" / "cron.py")
C = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(C)

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== 作业表 ===\n")

chk("五件事都有作业：恢复 / 升级 / 周报 / 巡检采集 / 巡检复核", set(C.JOBS) == {
    "data-steward-resume", "data-steward-escalate", "data-steward-weekly-report",
    "data-steward-metadata-sweep", "data-steward-metadata-review"},
    str(sorted(C.JOBS)))

# 巡检拆成两个作业：采集贵（连源库）、看待办便宜（只读治理库）。
# 合成一个的话，要么每 10 分钟去源库扫一遍，要么一天才发现一次待复核。
_sw, _rv = C.JOBS["data-steward-metadata-sweep"], C.JOBS["data-steward-metadata-review"]
chk("采集是 no_agent（比对是确定性的，不必点模型）", _sw.get("no_agent") is True)
chk("采集按天跑（连源库的事不该每分钟做）", "day" in _sw["schedule"], _sw["schedule"])
chk("复核走 monitor（没有待复核就整个跳过）",
    bool(_rv.get("monitor_script")) and not _rv.get("no_agent"))
chk("**复核的提示词明说不要自己改结论**（改不改是业务判断）",
    "不要自己改" in (_rv.get("prompt") or ""))

# 恢复要「等的人回了就往下走」，一小时太慢；升级阶梯按天算，一分钟太密。
chk("恢复是分钟级、升级是小时级（节奏对得上各自的语义）",
    "minute" in C.JOBS["data-steward-resume"]["schedule"]
    and "hour" in C.JOBS["data-steward-escalate"]["schedule"],
    f'{C.JOBS["data-steward-resume"]["schedule"]} / {C.JOBS["data-steward-escalate"]["schedule"]}')

print("\n=== 该不该点模型 ===\n")

# 催办是确定性阶梯判断 —— 走 no_agent 就不会每小时点一次模型。
chk("data-steward-escalate 不点模型（升级阶梯是确定性的）",
    C.JOBS["data-steward-escalate"].get("no_agent") is True)

# **周报是反例**：它写给人看、要给建议和理由，所以要点模型。
# 一周一次，成本可忽略 —— 「不点模型」在这里不是优点。
_wk = C.JOBS["data-steward-weekly-report"]
chk("周报点模型（要给建议和理由，不是播报）", not _wk.get("no_agent"))
# 但数字仍由脚本查库算：事实不该让模型编。
chk("周报的四段数字仍由脚本供（agent 模式下当 Script Output 注入）",
    _wk.get("script") == "data_steward_weekly_report.py")
chk("周报带了写提案的 skill", _wk.get("skills") == ["data-steward-stage-proposal"],
    str(_wk.get("skills")))
chk("提示词明说不要重算脚本给的数字",
    "不要重算" in (_wk.get("prompt") or ""))
# skill 是引导层，**限制不许写在里面**（铁律 1）——
# 写了也证明不了绕不过，真正的门是 gate 的 _silver_round_closed。
_skill = (ROOT / ".hermes" / "skills" / "data-steward-stage-proposal"
          / "SKILL.md").read_text(encoding="utf-8")
chk("skill 文件在仓库里（跟着代码走，不写进用户的 ~/.hermes）", bool(_skill))
chk("**skill 里没有写死限制**（那是门禁的事）",
    "你不可以" not in _skill and "禁止你" not in _skill)
chk("skill 点名了必须调的工具", "propose_stage_decision" in _skill)
# skill 目录靠 config 的 external_dirs 才被扫到 —— 路径不存在时
# Hermes 静默跳过，所以这条得钉住。
_cfg = (ROOT / ".hermes" / "home" / "config.yaml").read_text(encoding="utf-8")
chk("config 把仓库里的 skills 目录挂上了（否则静默扫不到）",
    "external_dirs" in _cfg and ".hermes/skills" in _cfg)

# 恢复必须让 Agent 自己再调一次工具（M5 驱动反转），所以要点模型。
# 但「每分钟点一次模型」显然不行 —— 靠 monitor_script 把它压回一次 SQL。
_res = C.JOBS["data-steward-resume"]
chk("恢复要点模型（Agent 自己再调工具，不是脚本代劳）",
    not _res.get("no_agent") and bool(_res.get("prompt")))
chk("**但靠 monitor 压住**：输出没变就整个跳过，不点模型",
    bool(_res.get("monitor_script")), str(_res.get("monitor_script")))
chk("恢复作业没有 script（monitor 的输出就是它的输入）",
    not _res.get("script"))

# monitor 源的输出必须按字节稳定 —— 带一个时间戳就等于每分钟点一次模型。
_mon = (ROOT / "ops" / "resumable.py").read_text(encoding="utf-8")
# 不许有**每次都变**的东西。但「等了几个整小时」是**慢变量** ——
# 同一小时内哈希不变（不重复唤醒），跨点变一次（失败的线自己重试）。
# 这一条是「一次没推成的唤醒把信号永久消费掉」的解，见 resumable.py 文档。
chk("monitor 源不打时间戳（否则哈希每次都变，等于没有 monitor）",
    "strftime" not in _mon and "datetime" not in _mon
    and "// unit" in _mon)
# monitor 输出必须让模型**不用猜**就能把动作重放一遍。
#
# 原来这里断言的是 `"IDENTITY_KEYS" in _mon` —— 那是当时实现的**代号**，
# 不是要保的属性。当时给的是 `<run_id>\t<tool>\t<对象>\t<等了多久>`，
# 模型得自己拆哪段是 asset、哪段是 key；live eval 实测：给了这样一行，
# 模型跑了 201 秒、12 次工具调用，一次都没调对（R6 §13）。
#
# 现在每行是 JSON，`args` 直接是**人批准的那一份参数**（比 IDENTITY_KEYS
# 更全）。所以断言改成验真实属性：结构化、带工具名、带参数、不带凭证。
chk("monitor 输出是结构化 continuation（不是让模型解析的人话日志）",
    "json.dumps" in _mon and '"tool"' not in _mon.split("def _line")[0]
    and "_line(" in _mon)
chk("每行带工具名与参数（模型照抄即可，不用猜身份字段）",
    "tool=" in _mon and "args=" in _mon)
chk("参数取自**人批准的那份票**，不是线上记的那份",
    "_approved_args" in _mon and "args_json FROM approvals" in _mon)
chk("**凭证不进 prompt**（这行会原样喂给模型）",
    "_CREDENTIAL_KEYS" in _mon and "_public_args" in _mon)

chk("**但有一个按格子的慢变量**（否则唤醒失败就再也醒不过来）",
    "_waited" in _mon and "RETRY_UNIT_S" in _mon)
chk("格子大小可调，但只影响重试节奏、不影响任何判断",
    "CLAW_RESUME_UNIT_SECONDS" in _mon and "3600" in _mon)

import subprocess as _sp3
_h1 = _sp3.run([sys.executable, str(ROOT / "ops" / "resumable.py")],
               capture_output=True, text=True, timeout=60).stdout
_h2 = _sp3.run([sys.executable, str(ROOT / "ops" / "resumable.py")],
               capture_output=True, text=True, timeout=60).stdout
chk("慢变量不影响同一小时内的稳定性（连跑两次仍逐字节相同）", _h1 == _h2)
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

_init = (ROOT / ".hermes" / "plugins" / "data-steward"
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

print("\n=== 定义变了不能只按名字跳过 ===\n")

# 造一个假的 Hermes cron 面：存量里躺着 data-steward-resume 的**老定义**
# （M4 形态：no_agent 脚本 data_steward_resume.py），其余两个与表一致。
# 只按名字判重的话，老作业会一直跑一个已经删掉的脚本，
# 失败只记在 Hermes 侧日志里 —— 我们这边什么都看不见。
import types as _types

_calls = {"created": [], "updated": []}
_fake = _types.ModuleType("cron.jobs")
_fake.parse_schedule = lambda s: {"raw": s}


def _consistent(name, job):
    return {"id": f"id-{name}", "name": name, "prompt": job.get("prompt") or "",
            "script": job.get("script"), "monitor_script": job.get("monitor_script"),
            "no_agent": bool(job.get("no_agent")),
            "skills": list(job.get("skills") or []),
            "schedule": _fake.parse_schedule(job["schedule"])}


_STALE = {"id": "id-data-steward-resume", "name": "data-steward-resume", "prompt": "",
          "script": "data_steward_resume.py", "monitor_script": None, "no_agent": True,
          "schedule": {"kind": "interval", "minutes": 1, "display": "every 1m"}}
_fake.list_jobs = lambda include_disabled=False: [
    _STALE,
    _consistent("data-steward-escalate", C.JOBS["data-steward-escalate"]),
    _consistent("data-steward-weekly-report", C.JOBS["data-steward-weekly-report"])]
_fake.create_job = lambda **kw: _calls["created"].append(kw)
_fake.update_job = lambda job_id, updates: _calls["updated"].append(
    (job_id, updates)) or {"id": job_id}

_pkg = _types.ModuleType("cron")
_pkg.jobs = _fake
_pkg.__path__ = []
_saved = {k: sys.modules.get(k) for k in ("cron", "cron.jobs")}
sys.modules["cron"], sys.modules["cron.jobs"] = _pkg, _fake
try:
    res_d = C.ensure_jobs(str(ROOT))
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

chk("老定义被发现并按表修正（不是按名字跳过）",
    len(_calls["updated"]) == 1 and _calls["updated"][0][0] == "id-data-steward-resume",
    str(res_d.get("updated")))
_upd = _calls["updated"][0][1] if _calls["updated"] else {}
chk("修正后的定义是 monitor 作业（老脚本被换掉，不是缝缝补补）",
    _upd.get("monitor_script") == "data_steward_resumable.py"
    and _upd.get("no_agent") is False and _upd.get("script") is None
    and bool(_upd.get("prompt")))
chk("修正**喊了出来**，且说清了哪些字段对不上",
    any("data-steward-resume" in x and "script" in x for x in res_d.get("updated", [])),
    str(res_d.get("updated")))
# 与表一致的原样保留；表里新增的作业**应当被创建**（这正是 ensure_jobs
# 的职责：加一行定义 = 下次启动自动登记）。
chk("与表一致的作业原样保留，没有被误改或重建",
    set(res_d.get("existing", [])) >= {"data-steward-escalate",
                                       "data-steward-weekly-report"})
chk("表里新增的作业被创建出来",
    {c["name"] for c in _calls["created"]} == {"data-steward-metadata-sweep",
                                               "data-steward-metadata-review"},
    str([c.get("name") for c in _calls["created"]]))

# 老定义指向的壳脚本已无人引用，必须删掉 —— 留着它，
# 「作业还在跑老脚本」这件事就永远查不出来。
chk("老壳脚本 data_steward_resume.py 已删除",
    not (HOME / "scripts" / "data_steward_resume.py").exists())

print("\n=== skills 也必须进 drift 比对 ===\n")

# 第二个会写进 cron 的字段。漏比它的后果与 `data-steward-resume` 那次一模一样：
# 存量作业带着旧的 skills 一直跑，而 `ensure_jobs` 看不出来。
# **这一组是「加字段忘了加比对」的哨兵** —— 不是在测某个具体的 skill。
_calls2 = {"created": [], "updated": []}
_probe = dict(C.JOBS["data-steward-weekly-report"], skills=["data-steward-stage-proposal"])
_stale_skills = _consistent("data-steward-weekly-report", _probe) | {"skills": ["旧的"]}
_fake.list_jobs = lambda include_disabled=False: [_stale_skills]
_fake.create_job = lambda **kw: _calls2["created"].append(kw)
_fake.update_job = lambda job_id, updates: _calls2["updated"].append(
    (job_id, updates)) or {"id": job_id}
_saved_jobs = C.JOBS
C.JOBS = {"data-steward-weekly-report": _probe}
sys.modules["cron"], sys.modules["cron.jobs"] = _pkg, _fake
try:
    res_s = C.ensure_jobs(str(ROOT))
finally:
    C.JOBS = _saved_jobs
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

chk("**skills 对不上会被判 drift**（漏比的话存量带着旧 skill 一直跑）",
    any("skills" in x for x in res_s.get("updated", [])), str(res_s))
chk("修正时把 skills 也按表写回去",
    (_calls2["updated"][0][1].get("skills") if _calls2["updated"] else None)
    == ["data-steward-stage-proposal"], str(_calls2["updated"])[:100])

# 顺序不同不算 drift —— 否则每次注册都白 update 一遍。
_fake.list_jobs = lambda include_disabled=False: [
    _consistent("data-steward-weekly-report", dict(_probe, skills=["b", "a"]))]
_calls2["updated"].clear()
C.JOBS = {"data-steward-weekly-report": dict(_probe, skills=["a", "b"])}
sys.modules["cron"], sys.modules["cron.jobs"] = _pkg, _fake
try:
    res_o = C.ensure_jobs(str(ROOT))
finally:
    C.JOBS = _saved_jobs
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
chk("**顺序不同不算 drift**（否则每次注册都白改一遍）",
    not _calls2["updated"] and res_o.get("existing") == ["data-steward-weekly-report"],
    str(res_o))

print("\n=== 模型也必须进 drift 比对（花费保护会静默跳过作业）===\n")

# Hermes 对没 pin 模型的 cron 作业有花费保护：全局模型一换，作业就被
# `[drift_skip:silent]` **静默跳过**，而且「告警只发一次，之后一直跳过」。
# 实测踩过：换掉一个欠费的模型之后整条恢复链停摆，日志里只有一行 ERROR。
# 这是「名字相同不代表定义相同」的第三次（script → skills → model）。
_calls3 = {"created": [], "updated": []}
_probe3 = dict(C.JOBS["data-steward-escalate"])
_stale3 = _consistent("data-steward-escalate", _probe3) | {"model": "老模型"}
_fake.list_jobs = lambda include_disabled=False: [_stale3]
_fake.create_job = lambda **kw: _calls3["created"].append(kw)
_fake.update_job = lambda job_id, updates: _calls3["updated"].append(
    (job_id, updates)) or {"id": job_id}
_saved_jobs3 = C.JOBS
C.JOBS = {"data-steward-escalate": _probe3}
_saved_home = os.environ.get("HERMES_HOME")
os.environ["HERMES_HOME"] = str(ROOT / ".hermes" / "home")
sys.modules["cron"], sys.modules["cron.jobs"] = _pkg, _fake
try:
    res_m = C.ensure_jobs(str(ROOT))
finally:
    C.JOBS = _saved_jobs3
    if _saved_home is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _saved_home
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

_cfg_model = C._current_model(str(ROOT / ".hermes" / "home"))[0]
chk("读得到当前配置的模型", bool(_cfg_model), str(_cfg_model))
chk("**pin 的模型与全局配置不符会被判 drift**（否则作业被静默跳过）",
    any("model" in x for x in res_m.get("updated", [])), str(res_m))
chk("修正时把模型 pin 成当前配置",
    (_calls3["updated"][0][1].get("model") if _calls3["updated"] else None)
    == _cfg_model, str(_calls3["updated"])[:90])

# 命名迁移必须保留作业身份、暂停状态，且重复注册不会重新创建。
_records = [_consistent(name.replace("data-steward-", "claw-", 1), job)
            | {"enabled": False} for name, job in C.JOBS.items()]
_original_ids = {j["id"] for j in _records}
_created_migration = []
_fake.list_jobs = lambda include_disabled=False: _records
_fake.create_job = lambda **kw: _created_migration.append(kw)
def _update_migration(jid, updates):
    rec = next(j for j in _records if j["id"] == jid)
    rec.update(updates)
    if isinstance(rec.get("schedule"), str):
        rec["schedule"] = _fake.parse_schedule(rec["schedule"])
    return rec
_fake.update_job = _update_migration
sys.modules["cron"], sys.modules["cron.jobs"] = _pkg, _fake
try:
    migrated = C.ensure_jobs(str(ROOT))
    again = C.ensure_jobs(str(ROOT))
    chk("旧名称原地迁移，保留 ID 与暂停状态",
        {j["id"] for j in _records} == _original_ids
        and {j["name"] for j in _records} == set(C.JOBS)
        and all(j["enabled"] is False for j in _records)
        and not migrated.get("errors"))
    chk("重复注册不重建、不重复更新", not _created_migration
        and not again["updated"] and len(again["existing"]) == len(C.JOBS))
    old = _consistent("claw-resume", C.JOBS["data-steward-resume"]) | {"enabled": True}
    old["id"] = "duplicate-legacy-id"
    _records.append(old)
    duplicate = C.ensure_jobs(str(ROOT))
    chk("新旧同时存在时停用旧作业，保留历史", old["enabled"] is False
        and old in _records and not _created_migration and not duplicate.get("errors"))
    # 更新失败必须报告，不能另建一份旧任务的替代品。
    _records[:] = [_consistent("claw-resume", C.JOBS["data-steward-resume"])]
    saved_jobs = C.JOBS
    C.JOBS = {"data-steward-resume": saved_jobs["data-steward-resume"]}
    _fake.update_job = lambda *args: None
    failed = C.ensure_jobs(str(ROOT))
    C.JOBS = saved_jobs
    chk("迁移失败不创建第二份作业", bool(failed.get("errors")) and not _created_migration)
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

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
