"""两个后端的表集合必须一致。

SQLite 的 DDL 和 Postgres 的 init 脚本是两份手写的东西，谁都不会
提醒另一边。少建一张表的后果分两种，**都很隐蔽**：

  · `GRANT ... ON <不存在的表>` 直接报错 → 整个 init 半途而废
  · 表不存在时查询被 except 吃掉 → 静默返回 0，看着像「没超预算」

`usage_ledger` 就是第二种：Postgres 侧压根没有这张表，
预算兜底（readme 5.6）读到的永远是 0。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def tables(text):
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", text))


sqlite_src = (ROOT / "plugins" / "datasteward_gate"
              / "approvals.py").read_text(encoding="utf-8")
pg_src = (ROOT / "infra" / "init-steward.sql").read_text(encoding="utf-8")

lite, pg = tables(sqlite_src), tables(pg_src)

print("\n=== 表集合 ===\n")
chk("SQLite 侧有表", len(lite) >= 10, f"{len(lite)} 张")
chk("Postgres 侧有表", len(pg) >= 10, f"{len(pg)} 张")
chk("**两边表集合一致**（加表要同步改两处）", lite == pg,
    f"只在 SQLite: {sorted(lite - pg)} · 只在 PG: {sorted(pg - lite)}")

print("\n=== 列集合也必须一致 ===\n")

# 只比表集合是不够的：`approvals` 两边都在、名字也对，但 PG 侧
# **少了 kind / options / question / evidence / abandoned_at /
# escalation_level 六列**（M5 才发现）。后果不是「少个字段」——
# `ask()` 的 INSERT 直接崩，Postgres 后端上提问、升级、放弃三条路全断，
# 而这一组当时是绿的。表级比对放过了它，所以补到列级。
def columns(text, table):
    m = re.search(r"CREATE TABLE IF NOT EXISTS\s+%s\s*\((.*?)\n\);" % table,
                  text, re.S)
    if not m:
        return None
    out = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        name = line.split()[0]
        if name.upper() in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
            continue
        out.append(name.lower())
    return out


col_drift = {}
for t in sorted(lite & pg):
    a, b = columns(sqlite_src, t), columns(pg_src, t)
    if a is None or b is None:
        col_drift[t] = "解析不出列"
        continue
    only_l, only_p = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    if only_l or only_p:
        col_drift[t] = f"只在 SQLite {only_l} · 只在 PG {only_p}"

chk("**每张表的列集合两边一致**（加列要同步改两处）", not col_drift,
    "; ".join(f"{t}: {d}" for t, d in col_drift.items())[:200])

# question 型是提问 / 阶段提案的载体，两边都得有那四列，
# 少一列就是「一个后端上提问根本发不出去」。
_q = set(columns(pg_src, "approvals") or [])
chk("question 型需要的列在 PG 侧齐了",
    {"kind", "options", "question", "evidence"} <= _q,
    str(sorted({"kind", "options", "question", "evidence"} - _q)))

# `decisions.chosen` 是「人选了哪个」的唯一落点。判分的 silver_gated
# 读它 —— 少一边就变成读不到。
chk("decisions.chosen 两边都有（判分读它）",
    "chosen" in (columns(sqlite_src, "decisions") or [])
    and "chosen" in (columns(pg_src, "decisions") or []))

print("\n=== 列级 INSERT 授权要盖住代码真的会写的列 ===\n")

# `GRANT INSERT (列...)` 漏一列的表现是运行时 InsufficientPrivilege，
# 而调用方通常把异常吃掉 —— 又是一次静默。
m = re.search(r"GRANT INSERT \(([^)]*)\)\s*\n?\s*ON approvals", pg_src, re.S)
granted_cols = {c.strip() for c in (m.group(1) if m else "").split(",") if c.strip()}
chk("Agent 能写的 approvals 列覆盖了 ask() 用到的",
    {"kind", "options", "question", "evidence"} <= granted_cols,
    str(sorted({"kind", "options", "question", "evidence"} - granted_cols)))
# 反向：Agent **不该**能改自己的升级层级或把待办标成已放弃 ——
# 那样就能绕开 WIP 限制。
chk("**但 escalation_level / abandoned_at 没授给 Agent**（否则可绕 WIP）",
    not ({"escalation_level", "abandoned_at"} & granted_cols),
    str(sorted({"escalation_level", "abandoned_at"} & granted_cols)))

print("\n=== GRANT 不能指向不存在的表 ===\n")

granted = set()
for m in re.finditer(r"GRANT [A-Z, ()a-z_]+ ON ([a-z_, ]+?) TO", pg_src):
    for t in m.group(1).split(","):
        t = t.strip()
        if t and not t.startswith("SEQUENCE"):
            granted.add(t)
missing = sorted(t for t in granted if t not in pg and t != "DATABASE")
chk("每条 GRANT 的表都真的建了（否则 init 脚本会半途而废）",
    not missing, str(missing))

print("\n=== 序列授权跟着自增主键走 ===\n")

# BIGSERIAL 会隐式建一个序列。只 GRANT INSERT 不 GRANT 序列，
# 插入时报 "permission denied for sequence" —— 而调用方通常把异常吃掉，
# 于是又变成一次静默的「什么都没记下来」。
serial = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)\s*\("
                        r"(?:[^;]*?)(?:BIGSERIAL|SERIAL)", pg_src))
seq_granted = set(re.findall(r"ON SEQUENCE ([a-z_]+)_(?:id|seq)_seq", pg_src))
ins_granted = {t for t in granted
               if re.search(r"GRANT [^;]*INSERT[^;]*ON [^;]*\b" + t + r"\b", pg_src)}
need = sorted((serial & ins_granted) - seq_granted)
chk("每张可插入的自增表都授了序列权限", not need, str(need))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
