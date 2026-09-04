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
