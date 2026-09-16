"""Identity / System Prompt 加载检查（第一批 A1）。

**只验一件事：模型这一轮实际收到的身份说明是新的那份。**
执行约束不在这里 —— 那是门禁的事（铁律 1），由 B 组的用例验。

不连数据库、不起 Docker：拿一个假的 ctx 走真正的 `register()`，
看落进 system prompt 的那段文字是什么。register() 里 cron 那段
失败会被自己吞掉，所以没有库也能跑完。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, ".hermes", "plugins", "data-steward")

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


class Ctx:
    """Hermes 那一侧的三个注册口，够用就行。"""

    def __init__(self):
        self.hooks, self.sections, self.tools = {}, {}, []

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_system_prompt_section(self, name, text):
        self.sections[name] = text

    def register_tool(self, *a, **k):
        self.tools.append((a, k))


print("\n=== Identity / System Prompt（A1）===\n")

spec = importlib.util.spec_from_file_location(
    "ds_plugin_probe", os.path.join(PLUGIN, "__init__.py"),
    submodule_search_locations=[PLUGIN])
mod = importlib.util.module_from_spec(spec)
sys.modules["ds_plugin_probe"] = mod
spec.loader.exec_module(mod)

ctx = Ctx()
mod.register(ctx)

guide = ctx.sections.get("data-steward-stop-points") or ""
check("身份说明真的注册进了 system prompt", bool(guide), f"{len(guide)} 字")
if not guide:
    sys.exit(1)

# --- 核心产物边界 ---
for term in ("Bronze", "画像", "Silver", "Knowledge SQL"):
    check(f"核心产物写明：{term}", term in guide)

# 产物清单里不能出现 Gold —— 它是下游的东西，写进来就是旧表述
bullets = [ln for ln in guide.splitlines() if ln.startswith("- **")]
deliverables = [b for b in bullets if any(t in b for t in ("Bronze", "Silver", "画像", "Knowledge SQL"))]
check("产物清单里没有 Gold（旧的核心交付表述已校正）",
      not any("Gold" in b for b in deliverables),
      "；".join(b for b in deliverables if "Gold" in b)[:80])

# --- 业务分析交下游，但湖内查询仍是自己的活 ---
check("点名把业务分析交下游", "Analytics" in guide and "下游" in guide)
check("点名不做的分析类型（归因 / 预测 / 排名 至少两类）",
      sum(t in guide for t in ("归因", "预测", "排名")) >= 2)
check("保留湖内查询 / JOIN / 统计 / 验证", "JOIN" in guide and "验证" in guide)

# --- 源只读、正式授权 ---
check("源系统只读", "只有读权限" in guide)
check("正式批准，不认邮件正文", "正式批准" in guide and "点链接" in guide)

# --- 逐人披露 ---
check("对外说明前按人核披露范围",
      "对外" in guide and ("该看到什么" in guide or "能看哪些资产" in guide))

# --- 事实源分工 ---
check("Memory 只留摘要与沟通偏好", "memory" in guide.lower() and "摘要" in guide)
check("冲突以 SQL / State 为准", "SQL / State 为准" in guide or "以 State" in guide)

# --- 成本护栏：这段每次请求都进 prompt ---
check("身份段长度可控（≤ 3500 字）", len(guide) <= 3500, f"{len(guide)} 字")

# --- 本批的三份 skill：prompt 指得到，文件真的在 ---
SKILLS = ("data-steward-task-entry", "data-steward-single-table-ingest",
          "data-steward-stage-proposal")
for name in SKILLS:
    md = os.path.join(ROOT, ".hermes", "skills", name, "SKILL.md")
    exists = os.path.isfile(md)
    check(f"skill 文件存在：{name}", exists, md if not exists else "")
    check(f"身份说明指向了 {name}", name in guide)
    if exists:
        txt = open(md, encoding="utf-8").read()
        check(f"{name} 有 frontmatter 且 name 对得上",
              txt.startswith("---") and f"name: {name}" in txt)

# skill 引用的工具必须真的存在 —— 引导模型去调一个不存在的工具，
# 它撞到的是「未声明 = 拒绝」，看起来像它不会干活。
#
# 反例词表（状态词、参数名、选项 key）**全部从代码里取**，不手写一份 ——
# 手写的那份会烂掉，然后这条检查变成「永远绿」。
import re                                                    # noqa: E402
from importlib import import_module                          # noqa: E402

tools_mod = import_module("ds_plugin_probe.tools")
registered = set(tools_mod._SCHEMAS)
vocab = set(registered)
for sch in tools_mod._SCHEMAS.values():
    vocab |= set((sch.get("parameters") or {}).get("properties") or {})
vocab |= set(getattr(tools_mod, "_TASK_STATES", {}))
sys.path.insert(0, os.path.join(ROOT, "plugins"))
try:
    pol = import_module("datasteward_gate.policy")
    vocab |= set(pol.POLICY)
    vocab |= {o["key"] for o in getattr(pol, "STAGE_OPTIONS", [])
              if isinstance(o, dict) and "key" in o}
except Exception as e:                                       # noqa: BLE001
    check("策略表可导入（工具词表来源）", False, f"{type(e).__name__}: {e}")
# Hermes 自带、不在本项目 schema 里的那几个
vocab |= {"skill_view", "skills_list", "memory", "todo", "todo_list", "clarify",
          "tool_search", "tool_describe", "tool_call"}

for name in SKILLS:
    md = os.path.join(ROOT, ".hermes", "skills", name, "SKILL.md")
    if not os.path.isfile(md):
        continue
    txt = open(md, encoding="utf-8").read()
    mentioned = set(re.findall(r"`([a-z][a-z0-9_]{4,})`", txt))
    mentioned |= set(re.findall(r"`([a-z][a-z0-9_]{4,})\(", txt))
    unknown = sorted(m for m in mentioned
                     if m not in vocab and not m.startswith("data-steward"))
    check(f"{name} 引用的名字都能在代码里找到", not unknown, "、".join(unknown))

# --- 身份说明不许搬进业务邮件（Snapshot 不泄身份） ---
cases = os.path.join(ROOT, "evals", "behavior", "cases")
leaked = []
for fn in sorted(os.listdir(cases)) if os.path.isdir(cases) else []:
    p = os.path.join(cases, fn)
    if not os.path.isfile(p):
        continue
    try:
        if "你是这家公司的数据管家" in open(p, encoding="utf-8").read():
            leaked.append(fn)
    except Exception:                                        # noqa: BLE001
        pass
check("身份说明没有被抄进 Snapshot 业务邮件", not leaked, "、".join(leaked))

print(f"\n{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("失败项：" + "、".join(bad))
sys.exit(1 if bad else 0)
