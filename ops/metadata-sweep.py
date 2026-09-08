#!/usr/bin/env python3
"""元数据巡检的外部入口（闭环 B）。

    python3 ops/metadata-sweep.py              # monitor 源：待复核清单，输出稳定
    python3 ops/metadata-sweep.py --sweep      # 真的巡一遍（**会连源库**）
    python3 ops/metadata-sweep.py --sweep --send   # 巡完把复核任务发给负责人
    python3 ops/metadata-sweep.py --reviews    # 看有哪些待复核

**默认那条路不连源库。** 它给 cron 当 monitor 用：每 tick 跑一次、
按字节哈希输出、没变就整个跳过 agent 运行。真正的采集要显式 `--sweep`
—— 巡检本身该便宜，点模型才贵。

分工（与 `resumable.py` 同款）：

    monitor 源   有没有待复核的结论 —— 只读治理库，稳定输出
    --sweep     去源库看变了没有 —— cron 的 script 位，定时跑
"""
import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]


def do_sweep(send: bool, source: str | None) -> int:
    import metadata_watch as W
    r = W.sweep(source=source)
    print(f"巡检 {r['checked']} 个资产")
    if r["errors"]:
        # **采集失败要说出来**，不能算成「没变化」——
        # 「看过了没变」和「没看成」是两回事，后者下次还得看。
        for a, e in r["errors"].items():
            print(f"  ⚠️ {a}: {e}")
    if not r["changed"]:
        print("  源库结构没有变化")
        return 0
    for c in r["changed"]:
        print(f"\n  📌 {c['asset']}")
        for key, what in c["changes"]:
            print(f"       {key}: {what}")
    if r["reviews"]:
        print(f"\n  受影响、需要复核的结论 {len(r['reviews'])} 条：")
        for x in r["reviews"]:
            print(f"       {x['asset']} · {x['entry']}（原为 {x['was_status']}）")
    else:
        print("\n  没有挂在这些表上的推断或确认 —— 无人需要复核")
    if send:
        _notify(r["reviews"])
    return 0


def _notify(reviews: list) -> None:
    """把复核任务发给该资产的负责人。

    **一人一封，不是一条一封** —— 同一个人名下十条结论要复核，
    发十封信是骚扰，而骚扰的结果是没人看。
    """
    if not reviews:
        return
    import catalog
    import notify
    by_person = {}
    for x in reviews:
        own = catalog.ownership(x["asset"]) or {}
        # **按表名猜出来的归属也用**，但那只是约定 —— 猜错了发给上一级，
        # 总比没人收到强（与 `resolve_approver_role` 的兜底同一条思路）。
        who = own.get("person") or notify.cfg("MAIL_OWNER") or ""
        if who:
            by_person.setdefault(who, []).append(x)
    n = notify.get()
    for who, items in by_person.items():
        lines = ["源库结构变了，下面这些结论需要你确认还成不成立：", ""]
        for x in items:
            lines.append(f"- {x['asset']} · {x['entry']}（原为 {x['was_status']}）")
            lines.append(f"  变化：{x['why'][:200]}")
        lines += ["", "**结论我没有自己改**：巡检只认定「事实变了」，",
                  "该不该改口径是业务判断，等你说。"]
        try:
            res = n.send_notice(who, f"[数据管家] {len(items)} 条结论需要复核",
                                "\n".join(lines))
            print(f"  {notify.sent_line(res, who)}（{len(items)} 条）")
        except Exception as e:                                # noqa: BLE001
            print(f"  ⚠️ 通知 {who} 失败：{type(e).__name__}: {str(e)[:60]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="元数据巡检")
    ap.add_argument("--sweep", action="store_true", help="真的巡一遍（连源库）")
    ap.add_argument("--send", action="store_true", help="把复核任务发给负责人")
    ap.add_argument("--source", help="只巡这一个源")
    ap.add_argument("--reviews", action="store_true", help="列出待复核")
    a = ap.parse_args(argv)

    import metadata_watch as W
    if a.sweep:
        return do_sweep(a.send, a.source)
    if a.reviews:
        rows = W.open_reviews()
        if not rows:
            print("没有待复核的结论")
            return 0
        print(f"待复核 {len(rows)} 条：")
        for r in rows:
            v = r["value"] if isinstance(r["value"], dict) else {}
            print(f"  {r['asset']} · {r['key']}（原为 {v.get('was_status', '?')}）")
            print(f"       {str(v.get('reason', ''))[:120]}")
        return 0

    # 默认：monitor 源。**只读治理库，输出必须稳定。**
    line = W.monitor_line()
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
