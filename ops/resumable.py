#!/usr/bin/env python3
"""哪些线现在可以往下走了 —— **给 Hermes cron 当 monitor 用**。

输出必须**稳定**：Hermes 按字节哈希这段输出，没变就整个跳过这次 agent 运行
（记一次 no_change，不点模型）。所以这里不许打时间戳、不许带耗时、
不许有任何每次都不一样的东西 —— 带一个时间戳就等于每分钟点一次模型。

输出为空 = 没有可推进的线。

## 为什么带一个「等了几小时」

Hermes 的 monitor 在**跑 agent 之前**就把新哈希存下（它自己的注释写着
理由：失败的 agent 运行不该对着同一份内容反复告警）。这条语义对告警是
对的，对我们不对 —— 一次没推成的唤醒会把「有活要干」这个信号消费掉，
那条线之后永远 `unchanged`，挂在 waiting_human 等一个不会再来的唤醒。
实测踩过：审批批了、输出非空、monitor 检测到变化跑了一次，没推成，
然后再也没有第二次。

修法不是去掉抑制（那就成了每分钟点一次模型），而是让输出**按小时慢变**：
同一条线在同一个整点内哈希不变（不重复唤醒），跨过整点就变一次
（失败的线每小时自己重试一次）。节奏与催办一致。

    python3 ops/resumable.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]


def _silver_ready() -> list:
    """轮开了、bronze 有表、口径也有了 —— 这些表可以洗了。

    **bronze 轮做完之后没有任何东西驱动进入 silver**：`claw-resume` 只推
    「有决定在等」的线，而清洗是个新动作、没有线在等它；周报一周一次。
    人点了 `start_silver` 之后这一棒没人接 —— 实测跑到这里就停住了，
    提案出了、口径记了、bronze 落了，然后什么都不再发生。

    monitor 的语义本来就是「有什么可以往下走了」，这也算一种。
    输出保持稳定：洗完一张就少一行（不会反复唤醒同一张）。
    """
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "plugins"))
        from datasteward_gate.approvals import open_store
        with open_store(readonly=True, init_schema=False) as st:
            if st.stage_choice("start_silver") is None:
                return []                      # 轮没开，不催
            done = {r[0] for r in st.db.execute(
                "SELECT asset FROM asset_semantics")} if hasattr(
                st.db, "execute") else set()
            synced = [r[0] for r in st.db.execute(
                "SELECT asset FROM sync_state WHERE last_synced_at IS NOT NULL")]
    except Exception:                                        # noqa: BLE001
        return []
    if not synced:
        return []
    try:
        import sync
        silver = set(sync._trino(
            "SELECT table_name FROM iceberg.information_schema.tables"
            " WHERE table_schema='silver'"))
    except Exception:                                        # noqa: BLE001
        return []
    out = []
    for asset in sorted(synced):
        if "." not in asset:
            continue
        src, tbl = asset.split(".", 1)
        if f"{src}__{tbl}" in silver:
            continue                          # 洗过了
        out.append(f"SILVER\tapply_cleaning_rule\t{src} {tbl}\t轮已开")
    return out


def _what(r) -> str:
    """这条线在处理什么。

    **身份字段要给全。** 门禁按 `IDENTITY_KEYS` 认「同一个动作」，
    模型恢复时得照着填才对得上票据。只给一半的后果实测过：
    `define_semantics` 的身份是 (asset, key)，而这里只输出了 asset，
    模型每次自己编一个 key —— 每次都是新动作、都要新审批，
    把 steward 的队列占满，那条线永远推不动。

    **但凭证不给**（dsn / 口令之类）：这行会进模型的 prompt，
    而参数本体留在库里，恢复时门禁会把人批准的那份回填。
    """
    p = r.get("params") or {}
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "plugins"))
        from datasteward_gate.policy import IDENTITY_KEYS
        keys = IDENTITY_KEYS.get(r.get("kind"))
    except Exception:                                        # noqa: BLE001
        keys = None
    if keys:
        parts = [str(p[k]) for k in keys if p.get(k)]
        if parts:
            return " ".join(parts)
    return (p.get("table") or p.get("asset") or p.get("source")
            or p.get("source_id") or "")


# 慢变量的**格子大小**。生产是一小时（与催办节奏一致）；
# 调试时设成 60，重试就变成每分钟一次，不用干等一个钟头。
# **只影响重试节奏，不影响任何判断** —— 所以它可以是环境变量，
# 不是门禁上的开关。
RETRY_UNIT_S = int(os.environ.get("CLAW_RESUME_UNIT_SECONDS", "3600") or 3600)


def _waited(r) -> str:
    """等了几个整格。**慢变量**：同一格内哈希不变，跨格变一次。

    这是「失败的唤醒不会把信号永久消费掉」的那一格 —— 见模块文档。
    """
    import time
    unit = max(1, RETRY_UNIT_S)
    tag = "h" if unit == 3600 else ("m" if unit == 60 else f"x{unit}")
    try:
        age = time.time() - float(r.get("updated_at") or 0)
    except Exception:                                        # noqa: BLE001
        return f"0{tag}"
    return f"{max(0, int(age // unit))}{tag}"


def main() -> int:
    import runs
    lines = []
    try:
        rows = runs.resumable(), runs.retryable()
    except Exception:                                        # noqa: BLE001
        # 库还没建表（全新环境）、或者临时读不到 —— **安静退出，退出码 0**。
        # monitor 崩掉每分钟就是一次错误 tick；而「什么都没有」和
        # 「查不了」对下游是同一个意思：这一分钟没有可推进的线。
        return 0
    for r in rows[0]:
        lines.append(f'{r["run_id"]}\t{r["kind"]}\t{_what(r)}\t{_waited(r)}')
    lines += _silver_ready()
    for r in rows[1]:
        lines.append(f'{r["run_id"]}\t{r["kind"]}\t{_what(r)}\t{_waited(r)}\tWIP')
    # 排序：`resumable()` 按「谁先批」排，那是**会变的**顺序，
    # 而哈希认字节。不排的话，两个人先后批准会让同一批线的输出抖动，
    # 白白唤醒一次模型。真正的先后由恢复时再查库决定。
    for line in sorted(lines):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
