"""bronze → silver 清洗（readme 5、7、16.1）。

    干净数据集 ──inject──► 脏数据（源，只读）
                              │ 抽取
                          bronze（与脏数据**逐行一致**）
                              │ 清洗（人确认后）
                          silver ◄──对比──► 干净数据集（ground truth）

三条不能破的规矩：

1. **不改源系统**（铁律 4）。修复只发生在 bronze → silver 的变换里。
2. **bronze 原样落地**。它是脏数据的副本，不是清洗结果；
   `16.1` 有一条断言就是「bronze 必须与脏数据逐行一致」。
3. **原值不丢**。每个被改过的列都保留 `<col>_raw` —— 洗错了还能回溯，
   而且「洗成这样对吗」这个问题需要前后对比才回答得了（停止点 3）。

## 哪些能自动，哪些必须问

这是本项目与普通 cleaning agent 的分水岭（16.2 第 10 类）：

    确定性变换（大小写归一、去重、去空格）  → 可自动，仍需 L2 确认规则
    改变数值或语义（补空值、改数量级、改日期）→ **一律问人，永不自动**

一个「把 NULL 填成 0」的 agent 看起来很能干，直到财务发现报表少了一个亿。
"""
import json
import re

SILVER = "iceberg.silver"
BRONZE = "iceberg.bronze"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# issue → (档位, 生成 SQL 表达式的函数, 说明)
#
# **三档，不是两档**（readme 16.2 那张表逐条对应）：
#
#   auto     确定性且不改变含义 —— 只有去重
#   propose  能给出具体变换，但**要人批准这条规则**才执行（提案标准化）
#   ask      没有可提的变换，只能问人
#
# 一开始把 propose 混进 auto，结果 eval 立刻抓到：
# `sellers.seller_city` 被自动归一了，而那一列根本没人要求动。
# 更糟的是 `lower()` 不折叠音标，`São Paulo` 洗完仍是 `são paulo` ——
# **规则没修好它声称要修的东西，却已经改了数据。**
AUTO, PROPOSE, ASK = "auto", "propose", "ask"
def _trim_lower(c):
    return f'lower(trim("{c}"))'


def _trim(c):
    return f'trim("{c}")'


def _numeric_or_null(c):
    return (f'''CASE WHEN regexp_like(trim("{c}"), '^-?[0-9]+(\\.[0-9]+)?$')'''
            f''' THEN trim("{c}") END''')


FIXES = {
    "primary_key_not_unique": (AUTO, None, "按主键去重 —— 确定性，不改变任何值"),
    # lake 侧全量确认过的重复键。源上只能采样，这条才是确定的
    "pk_unique_full":   (AUTO, None, "全量确认的重复主键，去重 —— 确定性"),
    "broken_foreign_key": (ASK, None,
                           "孤儿行是删是留、还是补父行，只有业务定得了"),
    "enum_drift":       (PROPOSE, _trim_lower,
                         "大小写与空格归一。标准形取哪个要人定"),
    "spelling_drift":   (PROPOSE, _trim_lower,
                         "折叠大小写与空格。**音标不折叠** ——"
                         "São Paulo 与 Sao Paulo 是不是同一个地方要人裁决"),
    "type_mismatch":    (PROPOSE, _numeric_or_null,
                         "非数字置空，原值进 _raw 列 —— 不猜它本来想写什么"),
    "constant_column":  (ASK, None, "整列同值，是否废弃字段要问业务"),
    "high_null_rate":   (ASK, None, "空值是否合法只有业务知道（16.2 第 10 类）"),
    "unit_outlier":     (ASK, None, "数量级异常改错比不改危害大"),
    "date_before_order": (ASK, None, "日期矛盾可能是补录，不能自动改"),
}


class CleanError(RuntimeError):
    pass


def _ident(x, what="标识符"):
    if not IDENT.match(str(x or "")):
        raise CleanError(f"{what}不合法：{x!r}")
    return x


# ---------------------------------------------------------------- 提案
def propose(bronze_table: str, findings: list) -> dict:
    """由 DQ findings 生成清洗提案。**只提案，不执行。**

    分三堆（见 FIXES 上方说明）：
        auto     可直接执行
        propose  有具体变换，但要人批准这条规则
        ask      没有可提的变换，只能问

    `auto + propose` 才是「有表达式的」；`ask` 永远没有表达式，
    因此不可能被误执行。
    """
    auto, prop, ask = [], [], []
    for f in findings:
        issue, col = f.get("issue"), f.get("column")
        if not col or col == "*":
            continue
        spec = FIXES.get(issue)
        if not spec:
            ask.append({"column": col, "issue": issue, "expr": None, "rule": None,
                        "tier": ASK, "why": "没有对应的修复方式，按不可自动处理"})
            continue
        tier, fn, why = spec
        item = {"column": col, "issue": issue, "tier": tier, "why": why,
                "expr": fn(col) if fn else None,
                "rule": f"{issue}__{col}" if tier != ASK else None}
        (auto if tier == AUTO else prop if tier == PROPOSE else ask).append(item)
    dedup = any(f.get("issue") in ("primary_key_not_unique", "pk_unique_full")
                for f in findings)
    n = len(auto) + len(prop) + len(ask)
    return {"bronze_table": bronze_table, "auto": auto, "propose": prop,
            "ask": ask, "dedup_by_pk": dedup,
            "question": (f"{bronze_table}：{len(auto)} 项可直接做，"
                         f"{len(prop)} 项待你批准规则，"
                         f"{len(ask)} 项需要你给口径。洗成这样对吗？")
                        if n else "无需清洗",
            "note": "propose 项批准后才执行；ask 项永不自动（readme 16.2）"}


# ---------------------------------------------------------------- 执行
def apply(bronze_table: str, plan: dict, columns: list, pk: str | None = None,
          silver_table: str | None = None) -> dict:
    """按提案建 silver 表。**原值一律保留在 `<col>_raw`。**

    `plan["auto"]` 之外的任何东西都不会被动 —— 需要问人的那些原样带过去。
    """
    import sync

    _ident(bronze_table, "bronze 表名")
    tgt = silver_table or bronze_table
    _ident(tgt, "silver 表名")
    # 只执行 auto，以及**已获批准**的 propose（调用方通过 approved_rules 指名）
    approved = set(plan.get("approved_rules") or [])
    items = list(plan.get("auto", [])) + [
        a for a in plan.get("propose", []) if a["rule"] in approved]
    fixed = {a["column"]: a for a in items if a.get("expr")}

    sel, applied = [], []
    for c in columns:
        _ident(c, "列名")
        if c in fixed:
            sel.append(f'{fixed[c]["expr"]} AS "{c}"')
            sel.append(f'"{c}" AS "{c}_raw"')          # 原值不丢
            applied.append({"column": c, "issue": fixed[c]["issue"],
                            "rule": fixed[c]["rule"], "expr": fixed[c]["expr"]})
        else:
            sel.append(f'"{c}"')

    src = f'{BRONZE}."{bronze_table}"'
    if plan.get("dedup_by_pk") and pk:
        _ident(pk, "主键列")
        body = (f'SELECT {", ".join(sel)} FROM (SELECT *, row_number() OVER '
                f'(PARTITION BY "{pk}" ORDER BY "{pk}") _rn FROM {src}) t '
                f'WHERE _rn = 1')
        applied.append({"column": pk, "issue": "primary_key_not_unique",
                        "rule": f"dedup__{pk}", "expr": "row_number() = 1"})
    else:
        body = f'SELECT {", ".join(sel)} FROM {src}'

    sync._trino(f"CREATE SCHEMA IF NOT EXISTS {SILVER}")
    sync._trino(f'DROP TABLE IF EXISTS {SILVER}."{tgt}"')
    sync._trino(f'CREATE TABLE {SILVER}."{tgt}" AS {body}')
    n = int(sync._trino(f'SELECT count(*) FROM {SILVER}."{tgt}"')[0])
    b = int(sync._trino(f"SELECT count(*) FROM {src}")[0])
    return {"silver_table": f'{SILVER}."{tgt}"', "rows": n, "bronze_rows": b,
            "applied": applied,
            "deduped": b - n if plan.get("dedup_by_pk") and pk else 0}


def before_after(bronze_table: str, silver_table: str, column: str,
                 limit: int = 100) -> dict:
    """前后对比样例（停止点 3 的交付物：**前后对比样例 100 行**）。

    只列真的变了的行 —— 一百行没变化的对比说明不了任何事。

    `_trino` 用 CSV_UNQUOTED 输出，值里带逗号会把列切错，
    因此在 SQL 侧用 chr(31) 拼成一列再拆 —— 不跟 CSV 抢分隔符。
    """
    import sync
    _ident(silver_table)
    _ident(column)
    sep = chr(31)
    tbl = SILVER + '."' + silver_table + '"'
    changed = 'WHERE "' + column + '_raw" IS DISTINCT FROM "' + column + '"'
    expr = ('concat(coalesce("' + column + '_raw", \'\'), chr(31),'
            ' coalesce("' + column + '", \'\'))')
    rows = sync._trino(f"SELECT {expr} FROM {tbl} {changed} LIMIT {int(limit)}")
    samples = []
    for line in rows:
        if sep in line:
            a, _, b = line.partition(sep)
            samples.append({"before": a, "after": b})
    total = int(sync._trino(f"SELECT count(*) FROM {tbl} {changed}")[0])
    return {"column": column, "samples": samples, "shown": len(samples),
            "changed_rows": total, "note": "只列真的变了的行"}

def register_rules(applied: list, ledger_ref_by_issue: dict) -> list:
    """把生效的规则登记进台账 —— **每条清洗规则都要标注它在补哪个洞**（7）。"""
    import ledger
    out = []
    for a in applied:
        ref = ledger_ref_by_issue.get(a["issue"])
        if not ref:
            continue
        out.append(ledger.add_rule(a["rule"], ref,
                                   f'{a["column"]} / {a["issue"]}',
                                   retire_when="源系统修复该字段后"))
    return out
