"""发布到 gold（readme 7 停止点 4）——**对外的那一层**。

silver 是我们自己复核用的，gold 是别人真正查的。两者的差别不只是
「又复制一份」，有两件事必须在这一步发生，否则前面的清洗与分类都白做：

1. **`_raw` 列不能跟着出去。** `clean.apply` 把原值留在 `<col>_raw` 是为了
   可复核（停止点 3 要给前后对比）。把它一起发布，分析师随手就能读到
   清洗前的脏值 —— 等于清洗没做。
2. **没分类不发布。** 遮蔽规则由分类推导（`policy_sync.MATRIX`）。
   一张没打标的表在 rules.json 里不会产生任何列规则，analyst 看到的是明文。
   所以「先打标、再发布」不是流程建议，是这个函数的**前置条件**。

这两条是操作本身的正确性前提，不是「Agent 该不该做」——后者在门禁里
（`publish_gold` = L3，Owner 审批）。别把两层混起来。
"""
import re

GOLD = "iceberg.gold"
SILVER = "iceberg.silver"
RAW_SUFFIX = "_raw"


class PublishError(RuntimeError):
    pass


def _ident(x, what="标识符"):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(x or "")):
        raise PublishError(f"非法{what}：{x!r}")
    return x


def silver_columns(silver_table: str) -> list:
    import sync
    _ident(silver_table, "silver 表名")
    return sync.lake_columns("silver", silver_table)


def publish_gold(silver_table: str, gold_table: str | None = None,
                 columns: list | None = None, classified_by=None) -> dict:
    """silver → gold。要求目标资产已有分类。

    `classified_by` 只用于报错信息里提示该找谁；分类本身在
    `policy_sync.classify`（L2）里做，这里只读。
    """
    import policy_sync

    _ident(silver_table, "silver 表名")
    tgt = _ident(gold_table or silver_table, "gold 表名")
    asset = f"gold.{tgt}"

    cls = policy_sync.classifications([asset]).get(asset)
    if not cls or not cls.get("level"):
        raise PublishError(
            f"{asset} 还没有分类，不能发布。没有分类就推不出遮蔽规则，"
            f"analyst 会直接读到明文。先让 steward 确认分类"
            f"（PII / Confidential / Internal / Public）。")

    # 点名列的检查放在连 Trino **之前**：它是纯参数判断，
    # 挂在网络后面的话，Trino 一挂它就变成「因为别的原因失败了」——
    # 断言看着还是绿的，实际这道闸门根本没被走到。
    if columns:
        for c in columns:
            _ident(c, "列名")
        leaked = [c for c in columns if c.endswith(RAW_SUFFIX)]
        if leaked:
            raise PublishError(
                f"这些是清洗前的原值列，不能发布到 gold：{leaked}。"
                f"要复核前后对比请查 silver。")

    avail = silver_columns(silver_table)
    if not avail:
        raise PublishError(f"{SILVER}.{silver_table} 不存在或没有列。")

    if columns:
        missing = [c for c in columns if c not in avail]
        if missing:
            raise PublishError(f"silver 里没有这些列：{missing}")
        picked = list(columns)
    else:
        picked = [c for c in avail if not c.endswith(RAW_SUFFIX)]

    dropped_raw = [c for c in avail if c.endswith(RAW_SUFFIX) and c not in picked]

    import sync
    sel = ", ".join(f'"{c}"' for c in picked)
    sync._trino(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")
    sync._trino(f'DROP TABLE IF EXISTS {GOLD}."{tgt}"')
    sync._trino(f'CREATE TABLE {GOLD}."{tgt}" AS'
                f' SELECT {sel} FROM {SILVER}."{silver_table}"')
    n = int(sync._trino(f'SELECT count(*) FROM {GOLD}."{tgt}"')[0])

    # 血缘：谁是这张 gold 表的来源。停止点 4 的交付物之一。
    with policy_sync._store(readonly=False) as st:
        st.remember(asset, "lineage:upstream", f"{SILVER}.{silver_table}",
                    classified_by or "publish_gold")

    masked = [c for c, lv in (cls.get("columns") or {}).items()
              if policy_sync.MATRIX.get(lv, {}).get("analyst") == "mask"
              and c in picked]
    return {"gold_table": f'{GOLD}."{tgt}"', "asset": asset, "rows": n,
            "columns": picked, "dropped_raw": dropped_raw,
            "classification": cls["level"], "masked_columns": sorted(masked),
            "upstream": f"{SILVER}.{silver_table}",
            "note": "遮蔽在 Trino 侧生效，需 policy_sync.sync 落盘并重载配置"}
