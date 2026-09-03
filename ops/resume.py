#!/usr/bin/env python3
"""恢复器：把所有「等的人已经回了」的任务各推一步。

由 scheduler 周期性调用，也可以手工跑。**它不知道有哪些 Pipeline** ——
执行器在各自模块里导入时注册（`runs.register`）。

    python3 ops/resume.py            # 推一轮
    python3 ops/resume.py --list     # 只看现状，不动
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

import runs                                                   # noqa: E402
import pipelines.ingest_table                                 # noqa: E402,F401  导入即注册


def main():
    listing = "--list" in sys.argv
    for rid in runs.reconcile_abandoned():
        print(f"  ⏹  {rid} 等的审批已放弃 → 转为已知阻塞项")
    s = runs.summary()
    print(f"任务：{s['total']} 条 —— "
          + " · ".join(f"{k} {v}" for k, v in s.items() if k != "total"))

    waiting = runs.by_status("waiting_human")
    for r in waiting:
        print(f"  ⏸  {r['run_id']:<28} 等 {r['waiting_on'] or '?'} "
              f"({r['kind']})  {(r['note'] or '')[:50]}")

    ready = runs.resumable()
    if not ready:
        print("没有可恢复的任务（没人回信 ≠ 出错）")
        return 0
    print(f"\n可恢复 {len(ready)} 条：")
    if listing:
        for r in ready:
            print(f"  ▶  {r['run_id']}  ({r['kind']})")
        return 0
    for out in runs.resume_all():
        print(f"  ▶  {out['run_id']:<28} -> {out['status']}"
              + (f"  {out.get('error', '')}" if out.get("error") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
