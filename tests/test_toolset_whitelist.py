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

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
