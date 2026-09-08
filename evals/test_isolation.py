"""断言体外隔离 —— 判分器不许 import 被测代码。

这条不是风格检查，是**证据有效性的前提**：
判分器一旦 import 了 services/ 或 plugins/，
它给出的 `Unsafe Write Rate = 0.0%` 就等于被测系统给自己打分。

与铁律 2（审批 callback 必须独立进程）是同一条理由。

跑法：python3 evals/test_isolation.py
"""
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

# 被测系统的模块名 —— evals/ 下一个都不许出现
SUBJECT = {"connector", "data_tools", "datasteward_gate", "approvals", "policy",
           "notify", "inbound", "sync", "tokens", "approval_callback", "memory",
           "persona", "ingest_file", "trajectory", "traj_eval", "services",
           "plugins", "pipelines"}
# 判分与汇总更严：只许标准库 —— 它们产出的数字要能当证据，
# 依赖越少，「谁给谁打分」越无从含糊
# **判分器**才是必须纯标准库的那一份：它产出的数字要能当证据，而且
# 要能离线跑。取证不在此列 —— 它得够得着事实实际存放的地方
# （`query_ledger` 只在 Postgres 那一侧有人写），psycopg 与 snapshot.py 同待遇。
STDLIB_ONLY = {"score.py", "aggregate.py", "score_behavior.py"}
ALLOWED_THIRD_PARTY = {"psycopg"}

fails, checked = [], 0
for f in sorted(HERE.glob("*.py")):
    checked += 1
    tree = ast.parse(f.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    bad = mods & SUBJECT
    if bad:
        fails.append(f"{f.name} import 了被测代码：{sorted(bad)}")
    if f.name in STDLIB_ONLY:
        extra = {m for m in mods if m in ALLOWED_THIRD_PARTY} | bad
        if extra:
            fails.append(f"{f.name} 必须只依赖标准库，却 import 了 {sorted(extra)}")

# runner 属于体内，必须在 tests/ 而不是 evals/
for name, why in (("run_eval_case.py", "它 import 被测代码"),
                  # 行为 eval 的驱动与状态注入同理。**注入必须体内**：
                  # 造一张「有效票」要用被测系统自己那份 action_hash，
                  # 在体外重写一份，注出来的票跟门禁认的就不是同一张。
                  ("run_behavior_case.py", "它起 Hermes 并调被测 handler"),
                  ("behavior_fixture.py", "算动作指纹必须用被测系统那份函数")):
    if not (ROOT / "tests" / name).exists():
        fails.append(f"体内代码应在 tests/{name} —— {why}，不能放 evals/")
    if (HERE / name).exists():
        fails.append(f"{name} 出现在 evals/ —— 体内代码越界")

for m in fails:
    print(f"  ❌ {m}")
print(f"隔离断言：检查 {checked} 个文件，{'全部通过' if not fails else f'{len(fails)} 项失败'}")
sys.exit(1 if fails else 0)
