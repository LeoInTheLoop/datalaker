"""元数据巡检：**主动去看源库变了没有**（闭环 B）。

一句话立在前面，它是这条闭环最容易犯的错：

> **读自己的档案可以少访问源库；但发现源库变化，必须有新的外部观测。**

档案只能省掉重复访问。以为存了档案就「知道」源库变了，是错的 ——
所以这个模块**一定会连源库**，而且这是它存在的全部理由。

## 为什么不去改 `sync._schema_hash`

`sync` 里那个指纹只含列名+类型，改它会让存量 `sync_state` 集体报
SchemaDrift（`catalog.record_observed` 的注释里记着这笔账）。但巡检
**不需要**它：`asset_catalog` 已经把列名、类型、可空性、主键、外键
五项都存成了 observed 行，而 `catalog_put` 本来就按内容指纹判重 ——
内容变了它自己会写新行并把旧行 `superseded_by`。

于是「发现变化」这件事天然落在档案上，两条路各管各的：

    sync._schema_hash   同步前的守门：结构变了就别闷头往下灌
    asset_catalog       认知的基线：变了什么、什么时候变的、谁的结论因此失效

## 发现之后做什么

**巡检只负责认定「事实变了」，不负责决定「该怎么办」** —— 后者要问人。
所以它把受影响的推断与确认标成待复核（`kind='review'` 的条目），
并交给调用方去发通知。

    python3 ops/metadata-sweep.py            # monitor 用：待复核清单，稳定
    python3 ops/metadata-sweep.py --sweep    # 真的巡一遍（会连源库）

**不叫 `inspect.py`**：那是标准库的名字，而 `services/` 在 `sys.path`
前面 —— 叫那个名字会把标准库顶掉，好几处测试正用着 `inspect.getsource`。
"""
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

# 巡检只看这些 kind 的 observed 条目 —— 它们是「源库里到底有什么」。
WATCHED = ("schema", "foreign_keys")

# 变化影响谁：结构变了，挂在这张表上的推断与确认都要重看。
# **不包括 observed**：那是事实，事实变了就是变了，不需要「复核」。
AFFECTED_STATUS = ("inferred", "confirmed")


def _store(readonly=False):
    from datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=True)


def known_assets(source: str | None = None) -> list:
    """档案里已经有 observed 记录的资产。**巡检只看已经建过档的** ——
    没建过档的表不在认知范围内，那是「发现」的事，不是「巡检」的事。"""
    with _store(readonly=True) as st:
        rows = st.catalog(None, status="observed", limit=5000)
    out = sorted({r["asset"] for r in rows
                  if r["kind"] in WATCHED and "." in r["asset"]})
    if source:
        out = [a for a in out if a.split(".", 1)[0] == source]
    return out


def _current(st, asset: str) -> dict:
    """档案里这张表现在的 observed 内容（kind.key -> value）。"""
    cur = {}
    for r in st.catalog(asset, status="observed", limit=200):
        if r["kind"] in WATCHED and r["asset"] == asset:
            cur[f'{r["kind"]}.{r["key"]}'] = r["value"]
    return cur


def _diff(before: dict, after: dict) -> list:
    """哪些条目变了。返回 [(条目, 人话)]。

    **说清楚变的是什么**，不是「schema.columns 变了」这种没法行动的话 ——
    收到通知的人要据此判断自己的口径还成不成立。
    """
    out = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if key == "schema.columns":
            bn = {c[0]: c for c in (b or [])}
            an = {c[0]: c for c in (a or [])}
            added = sorted(set(an) - set(bn))
            gone = sorted(set(bn) - set(an))
            changed = [c for c in sorted(set(bn) & set(an)) if bn[c] != an[c]]
            bits = []
            if added:
                bits.append(f"新增列 {'、'.join(added)}")
            if gone:
                bits.append(f"**删除列** {'、'.join(gone)}")
            for c in changed:
                bits.append(f"{c} 由 {bn[c][1]}/{'可空' if bn[c][2] == 'YES' else '非空'}"
                            f" 改为 {an[c][1]}/{'可空' if an[c][2] == 'YES' else '非空'}")
            out.append((key, "；".join(bits) or "列定义有变"))
        elif key == "foreign_keys.declared":
            bs = {tuple(x) for x in (b or [])}
            as_ = {tuple(x) for x in (a or [])}
            bits = []
            for f in sorted(as_ - bs):
                bits.append(f"新增外键 {f[0]}.{f[1]}→{f[2]}.{f[3]}")
            for f in sorted(bs - as_):
                bits.append(f"**删除外键** {f[0]}.{f[1]}→{f[2]}.{f[3]}")
            out.append((key, "；".join(bits) or "外键有变"))
        elif key == "schema.primary_key":
            out.append((key, f"主键由 {b or '（无）'} 改为 {a or '（无）'}"))
        else:
            out.append((key, f"{b} → {a}"))
    return out


def affected(asset: str) -> list:
    """这张表上有哪些推断与确认，会因为结构变化而需要复核。"""
    with _store(readonly=True) as st:
        rows = st.catalog(asset, limit=500)
    return [r for r in rows if r["status"] in AFFECTED_STATUS
            and r["kind"] != "review"]


def sweep(assets: list | None = None, source: str | None = None) -> dict:
    """巡一遍：重新采集 → 与档案比对 → 变了就标记受影响的结论。

    **一张表出错不中断整轮** —— 一个源连不上不该让别的源也不巡。
    """
    targets = assets if assets is not None else known_assets(source)
    changed, errors, reviews = [], {}, []
    for asset in targets:
        if "." not in asset:
            continue
        src, tbl = asset.split(".", 1)
        with _store(readonly=True) as st:
            before = _current(st, asset)
        try:
            import catalog
            import connector
            meta = connector.describe_table(src, tbl)   # ← 唯一碰源库的一步
        except Exception as e:                                # noqa: BLE001
            errors[asset] = f"{type(e).__name__}: {str(e)[:90]}"
            continue
        # **采不到列不等于「表有 0 列」**（describe_table 对不存在的表返回空）。
        # 原来有档、这次读不到，是要报出来的事 —— 但**不自己判定是「表被删了」**：
        # 权限变化、连接抖动看起来一模一样，而误报「表没了」会引发一堆
        # 不必要的复核。如实说「原来有档，这次读不到」，让人去看。
        if not (meta.get("columns") or []):
            errors[asset] = ("原来有档、这次一列都没读到 —— 表可能被删了，"
                             "也可能是权限或连接问题，需要人看一眼"
                             if before else "采不到任何列（表不存在或无权限）")
            continue
        try:
            catalog.observe(src, tbl)
        except Exception as e:                                # noqa: BLE001
            errors[asset] = f"{type(e).__name__}: {str(e)[:90]}"
            continue
        with _store(readonly=True) as st:
            after = _current(st, asset)
        d = _diff(before, after)
        if not d:
            continue
        # **第一次建档不算「变化」**：before 是空的，那是从无到有。
        if not before:
            continue
        changed.append({"asset": asset, "changes": d})
        reviews += _mark_for_review(asset, d)
    return {"checked": len(targets), "changed": changed,
            "reviews": reviews, "errors": errors}


def _mark_for_review(asset: str, changes: list) -> list:
    """把受影响的推断与确认标成待复核。

    **不改它们本身** —— 推断和确认是历史事实，巡检没资格判它们失效。
    另写一条 `kind='review'`，说明「因为什么、要复核什么」，
    人看过之后再决定是改口径还是维持。
    """
    hits = affected(asset)
    if not hits:
        return []
    why = "；".join(f"{k}: {t}" for k, t in changes)[:300]
    out = []
    with _store() as st:
        for r in hits:
            key = f'{r["kind"]}.{r["key"]}'
            st.catalog_put(
                asset, "review", key,
                {"reason": why, "was_status": r["status"],
                 "was_value": r["value"]},
                "inferred", "system:inspect",
                evidence={"trigger": "metadata_sweep",
                          "changed": [k for k, _ in changes]})
            out.append({"asset": asset, "entry": key,
                        "was_status": r["status"], "why": why})
    return out


def open_reviews(asset: str | None = None) -> list:
    """还没处理的复核任务。**已解决的会被 confirm/refute 取代**，
    所以这里只查当前有效的那些。"""
    with _store(readonly=True) as st:
        rows = st.catalog(asset, limit=1000)
    return [r for r in rows if r["kind"] == "review"
            and r["status"] == "inferred"]


def resolve_review(asset: str, entry: str, actor: str, keep: bool,
                   note: str = "") -> bool:
    """人看过了。`keep=True` 维持原结论，`False` 判它失效。

    两种都要留痕 —— 「看过并认为仍然成立」和「没人看过」是两回事，
    而后者会在下一轮巡检里再冒出来。
    """
    from datasteward_gate.approvals import open_store
    with open_store(readonly=False, init_schema=True) as st:
        rid = st.catalog_put(
            asset, "review", entry,
            {"resolved": True, "keep": keep, "note": note[:300]},
            "confirmed" if keep else "refuted", actor,
            evidence={"resolved_at": time.time()})
    return bool(rid)


def monitor_line() -> str:
    """给 cron monitor 的输出。**稳定**：没有待复核就是空。

    与 `ops/resumable.py` 同款约束：不许打时间戳、不许带耗时 ——
    每 tick 都不一样的输出等于每分钟点一次模型。
    """
    rows = open_reviews()
    if not rows:
        return ""
    lines = sorted(f'{r["asset"]}\t{r["key"]}' for r in rows)
    return "\n".join(lines)
