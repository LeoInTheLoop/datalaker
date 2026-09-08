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
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]


def _silver_ready() -> list:
    """轮开了、bronze 有表、口径也有了 —— 这些表可以洗了。

    **bronze 轮做完之后没有任何东西驱动进入 silver**：`data-steward-resume` 只推
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
        out.append(_line(kind="silver_ready", tool="apply_cleaning_rule",
                         args={"source": src, "table": tbl},
                         note="清洗轮已开，这张表还没洗"))
    return out


def _public_args(d) -> dict:
    """能进 prompt 的那部分参数 —— **凭证一律不给**。

    这一行会原样进模型的 prompt。dsn / 口令留在库里，
    恢复时门禁会把人批准的那份回填（`_replay_approved_args`）。
    """
    from datasteward_gate.approvals import _CREDENTIAL_KEYS
    return {k: v for k, v in (d or {}).items()
            if str(k).lower() not in _CREDENTIAL_KEYS}


def _approved_args(approval_id):
    """**人批准的那一份参数。** 恢复要重放的是它，不是线上记的那份。

    两者通常一样，但票据是权威：`amend_pending` 改过的、
    或者线是按旧参数建的时候，只有票里那份是人看过的。
    """
    if not approval_id:
        return None
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "plugins"))
        from datasteward_gate.approvals import open_store, rows_of
        with open_store(readonly=True, init_schema=False) as st:
            row = rows_of(st, "SELECT args_json FROM approvals WHERE id = {0}",
                          (approval_id,))
        return _public_args(json.loads(row[0][0])) if row else None
    except Exception:                                        # noqa: BLE001
        return None


def _line(**kw) -> str:
    """一条 continuation。**JSON，不是人话日志。**

    原来这里输出的是 `<run_id>\t<tool>\t<对象>\t<等了多久>`，模型得自己猜
    哪段是 asset、哪段是 key、`30h` 是什么。live eval 实测：给了这样一行，
    模型跑了 201 秒、12 次工具调用，**一次都没调对那个工具**（R6 §13）。

    改成机器直接能吃的：工具名、参数、票据、状态各一个字段。
    模型的活从「解析日志」变成「照抄参数再调一次」——
    而它照抄得准不准也不再要紧，门禁会用票里那份回填。

    `sort_keys` + 无时间戳：输出要**逐字节稳定**，monitor 靠哈希抑制重复唤醒，
    抖一下就等于每分钟点一次模型。
    """
    return json.dumps(kw, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


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
        lines.append(_line(
            kind="approved", run_id=r["run_id"], tool=r["kind"],
            args=_approved_args(r.get("waiting_on"))
            or _public_args(r.get("params")),
            approval_id=r.get("waiting_on"), waited=_waited(r)))
    lines += _silver_ready()
    for r in rows[1]:
        # WIP 挡回来的：**没有票在等**，等的是别人的待办降下来。
        lines.append(_line(
            kind="blocked_by_wip", run_id=r["run_id"], tool=r["kind"],
            args=_public_args(r.get("params")), waited=_waited(r)))
    # 排序：`resumable()` 按「谁先批」排，那是**会变的**顺序，
    # 而哈希认字节。不排的话，两个人先后批准会让同一批线的输出抖动，
    # 白白唤醒一次模型。真正的先后由恢复时再查库决定。
    for line in sorted(lines):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
