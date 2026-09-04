#!/usr/bin/env python3
"""哪些线现在可以往下走了 —— **给 Hermes cron 当 monitor 用**。

输出必须**稳定**：Hermes 按字节哈希这段输出，没变就整个跳过这次 agent 运行
（记一次 no_change，不点模型）。所以这里不许打时间戳、不许带耗时、
不许有任何每次都不一样的东西 —— 带一个时间戳就等于每分钟点一次模型。

输出为空 = 没有可推进的线。

    python3 ops/resumable.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]


def main() -> int:
    import runs
    lines = []
    for r in runs.resumable():
        p = r.get("params") or {}
        what = p.get("table") or p.get("asset") or p.get("source") or ""
        lines.append(f'{r["run_id"]}\t{r["kind"]}\t{what}')
    for r in runs.retryable():
        p = r.get("params") or {}
        what = p.get("table") or p.get("asset") or p.get("source") or ""
        lines.append(f'{r["run_id"]}\t{r["kind"]}\t{what}\tWIP')
    # 排序：`resumable()` 按「谁先批」排，那是**会变的**顺序，
    # 而哈希认字节。不排的话，两个人先后批准会让同一批线的输出抖动，
    # 白白唤醒一次模型。真正的先后由恢复时再查库决定。
    for line in sorted(lines):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
