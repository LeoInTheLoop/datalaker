"""工具白名单断言（readme 4.6 支柱一）。

这是**安全主防线**：不注入 prompt 的工具，模型压根不知道它存在。
gate 的 deny by default 只是兜底——配置会漏，代码不会。

不依赖 Docker：直接校验配置文件。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "infra", "hermes", "config.yaml")

# 能执行任意命令 / 出网 / 读写文件 / 派生不受约束的子 agent
DANGEROUS = {
    "terminal", "code_execution", "computer_use", "browser", "file", "delegation",
}
# 本项目实际需要的
EXPECTED = {"clarify", "memory", "todo"}

ok, bad = [], []


def check(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== 工具白名单（安全主防线）===\n")

check("配置文件存在", os.path.exists(CFG), CFG)
if not os.path.exists(CFG):
    sys.exit(1)

try:
    import yaml
    cfg = yaml.safe_load(open(CFG, encoding="utf-8"))
except ImportError:
    # 无 yaml 时退化为文本解析，保证 CI 不因依赖失败
    txt = open(CFG, encoding="utf-8").read()
    import re
    def _block(key):
        m = re.search(rf"{key}:\s*\n((?:\s+-\s+\S+.*\n)+)", txt)
        return set(re.findall(r"-\s+([a-z_]+)", m.group(1))) if m else set()
    cfg = {"agent": {"enabled_toolsets": list(_block("enabled_toolsets")),
                     "disabled_toolsets": list(_block("disabled_toolsets"))}}

ac = cfg.get("agent") or {}
en = set(ac.get("enabled_toolsets") or [])
di = set(ac.get("disabled_toolsets") or [])

check("白名单是显式的（不是空 = 全开）", bool(en), f"{sorted(en)}")
check("白名单只含预期 toolset", en == EXPECTED, f"多出 {sorted(en - EXPECTED)}" if en - EXPECTED else "")

for t in sorted(DANGEROUS):
    check(f"高危 toolset 未启用: {t}", t not in en)

check("高危 toolset 全部显式列入 disabled（表明是刻意而非遗漏）",
      DANGEROUS <= di, f"漏列 {sorted(DANGEROUS - di)}" if DANGEROUS - di else "")
check("白名单规模合理（≤5 个 toolset）", len(en) <= 5, f"{len(en)} 个")

print("\n=== 打开的工具集必须在 policy 里声明 ===\n")

# **打开了却不声明 = 打开了个寂寞。** 模型一调就撞「未声明 = L4 拒绝」，
# 而那条拒绝消息还在教它「请在 policy.py 中显式声明」—— 它当然做不到。
# 实测撞在 memory 上：config 里 enable 了跨会话记忆，门禁却把它拒了。
import re as _re2                                             # noqa: E402
import sys as _s2                                             # noqa: E402
import pathlib as _p2                                         # noqa: E402
_ROOT2 = _p2.Path(__file__).resolve().parent.parent
_s2.path.insert(0, str(_ROOT2 / "plugins"))
from datasteward_gate.policy import POLICY as _POL            # noqa: E402

_cfg2 = (_ROOT2 / ".hermes" / "home" / "config.yaml").read_text(encoding="utf-8")
_m2 = _re2.search(r"^\s+enabled_toolsets:\s*$(.*?)(?=^\s+\w+:|\Z)",
                  _cfg2, _re2.M | _re2.S)
_enabled = _re2.findall(r"^\s+-\s+(\w+)", _m2.group(1) if _m2 else "", _re2.M)
# claw 是我们自己的工具集，逐个工具已在 policy 里；这里只查 Hermes 自带的。
_hermes_sets = [t for t in _enabled if t != "claw"]
_undeclared2 = [t for t in _hermes_sets if t not in _POL]
check("**enabled_toolsets 里的 Hermes 工具都在 policy 里声明了**",
      not _undeclared2,
      f"打开了却没声明：{_undeclared2}（模型一调就被自己的门禁拒）")
check("它们都是 L0（只碰 Agent 自己的东西，不碰源数据）",
      all(_POL[t][0] == 0 for t in _hermes_sets if t in _POL),
      str({t: int(_POL[t][0]) for t in _hermes_sets if t in _POL}))

print("\n=== 声明了级别，就得真有那个工具 ===\n")

# **这个项目在同一个形状上栽了四次**：`sql_query` / `connect_source` /
# `describe_asset` / `define_semantics` 都曾经「在 policy.py 里有一行级别
# 声明，但 tools.py 里没有实现」。后果比想象的糟：模型找不到工具时会
# **拿别的东西凑合** —— define_semantics 缺席时它把业务口径记进了 Hermes
# 自己的 memory，看着像记住了，项目的 asset_semantics 里一条都没有，
# 换个会话换台机器就全丢。
#
# 还没实现的必须**显式列出来**。列表在这里而不是在 policy.py：
# 欠账要放在会被跑到的地方。
NOT_YET_IMPLEMENTED = {
    "run_dq_check",                 # 质量检查（pipelines 里有逻辑，没接成工具）
    "search_asset", "get_lineage",  # 元数据检索（readme 3/13 的 OM 读侧）
    "full_refresh",                 # 整表刷新
    "confirm_column_mapping",       # 8.2 导出列名映射
    "connect_saas_control_plane", "dump_saas_permissions",   # 8.2 SaaS 控制面
}

import importlib.util as _ilu3                               # noqa: E402
_sp = _ilu3.spec_from_file_location(
    "claw_tools_probe", _ROOT2 / ".hermes" / "plugins" / "claw" / "tools.py",
    submodule_search_locations=[str(_ROOT2 / ".hermes" / "plugins" / "claw")])
_s2.path.insert(0, str(_ROOT2 / ".hermes" / "plugins"))
import claw.tools as _T3                                     # noqa: E402

# Hermes 自带的那些不需要我们实现 —— 它们的实现在 Hermes 里。
_HERMES_OWN = {"tool_search", "tool_describe", "tool_call", "skill_view",
               "skills_list", "memory", "todo", "clarify", "sql_query"}
_declared = {t for t, (lv, _) in _POL.items() if int(lv) < 4}
_ghost = sorted(_declared - set(_T3._HANDLERS) - _HERMES_OWN - NOT_YET_IMPLEMENTED)
check("**没有「声明了级别却不存在」的工具**（未实现的必须显式列出）",
      not _ghost,
      f"这些只有声明没有实现：{_ghost} —— 要么补实现，要么加进 NOT_YET_IMPLEMENTED")

# 反过来：欠账清单不许留着已经补上的，否则它会慢慢变成一张谎话表。
_stale = sorted(NOT_YET_IMPLEMENTED & set(_T3._HANDLERS))
check("欠账清单里没有已经实现了的（清单要跟着代码走）", not _stale, str(_stale))

# 每个实现了的工具也必须有 schema，否则 Hermes 那边下发不出去。
_no_schema = sorted(set(_T3._HANDLERS) - set(_T3._SCHEMAS))
check("每个 handler 都有对应的 schema", not _no_schema, str(_no_schema))

print("\n=== 身份字段得是工具真有的那个参数名 ===\n")

# **想当然的代价**：给 `ingest_export` 声明身份时写了 ("source","table")，
# 而它的参数名其实是 `saas_source` —— 于是身份字段只匹配上一半，
# 恢复时指纹对不上，人批过的又白批了。而这种错**看起来完全正常**：
# 声明有了、测试也过，只有真跑一次带附件的接入才会暴露。
from datasteward_gate.policy import IDENTITY_KEYS as _IK          # noqa: E402

_bad_ident = {}
for _t, _keys in _IK.items():
    _sch = _T3._SCHEMAS.get(_t)
    if not _sch:
        continue                       # 还没实现的工具没 schema，跳过
    _props = set((_sch.get("parameters") or {}).get("properties") or {})
    _miss = [k for k in _keys if k not in _props]
    if _miss:
        _bad_ident[_t] = _miss
check("**身份字段都是工具 schema 里真有的参数**（写错名字 = 恢复永远对不上）",
      not _bad_ident,
      "; ".join(f"{t} 里没有 {m}" for t, m in _bad_ident.items()))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
