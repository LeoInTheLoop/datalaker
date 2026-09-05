#!/usr/bin/env python3
"""bronze 轮的**终点**：全部线到终态，或天数上限 —— 二者先到为准。

v1 的大 case 跑到 day 12 就散场，没有「这一轮怎么算结束」。
真实的一轮必须有终点，到了要出阶段报告，然后**安静下来** ——
停得下来也是能力，跟停止点判据是同一条原则（下一步需要的判断
不在当前上下文里）。

**输出必须稳定**：它同时给 Hermes cron 当 monitor 源用（与
`ops/resumable.py` 同款）。不许打时间戳、不许带耗时 ——
带一个时间戳就等于每分钟唤醒一次模型。

    python3 ops/stage-report.py            # monitor 用：一行状态，稳定
    python3 ops/stage-report.py --report   # 人看的：完整阶段报告
    python3 ops/stage-report.py --send     # 发给拍板人
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

# 一轮的天数上限。到点了就收，哪怕还有人没回 ——
# 无限等下去不是尽责，是没有终点。
MAX_DAYS = float(os.environ.get("CLAW_ROUND_MAX_DAYS", "15"))

TERMINAL = ("done", "abandoned", "failed")
ACTIVE = ("running", "waiting_human")


def round_state() -> dict:
    """这一轮走到哪了。**查不到就当没结束** —— 缺测量值按最坏算。"""
    try:
        import runs
    except Exception:                                         # noqa: BLE001
        return {"known": False}

    try:
        rows = {s: runs.by_status(s) for s in TERMINAL + ACTIVE}
    except Exception:                                         # noqa: BLE001
        # 全新库里 runs 表还没建。与 resumable.py 同款：安静返回，
        # 不让 monitor 每分钟报一次错。
        return {"known": False}

    active = sum(len(rows[s]) for s in ACTIVE)
    terminal = sum(len(rows[s]) for s in TERMINAL)
    if active + terminal == 0:
        return {"known": True, "started": False, "active": 0, "terminal": 0}

    # 天数按**最早那条线**算：一轮从第一条线开的那一刻起算。
    starts = [r["created_at"] for s in TERMINAL + ACTIVE for r in rows[s]]
    import time
    days = (time.time() - min(starts)) / 86400.0 if starts else 0.0
    return {
        "known": True, "started": True,
        "active": active, "terminal": terminal,
        "done": len(rows["done"]), "abandoned": len(rows["abandoned"]),
        "failed": len(rows["failed"]),
        "days": days,
        "all_terminal": active == 0,
        "over_deadline": days >= MAX_DAYS,
    }


def is_over(st: dict | None = None) -> bool:
    """这一轮结束了没有。**两个判据先到为准**（case 的 stop 段）。"""
    st = st if st is not None else round_state()
    if not st.get("known") or not st.get("started"):
        return False
    return bool(st.get("all_terminal") or st.get("over_deadline"))


def monitor_line(st: dict | None = None) -> str:
    """给 cron monitor 的一行。**稳定**：只在轮真的结束时才变。

    没结束时输出空 —— 与 `resumable.py` 一样，空 = 这一分钟没事可做。
    结束时输出一行固定文本（不含天数，天数每分钟都在变，
    会让哈希每 tick 都不一样，等于没有 monitor）。
    """
    st = st if st is not None else round_state()
    if not is_over(st):
        return ""
    why = "全部线到终态" if st.get("all_terminal") else f"到达 {MAX_DAYS:g} 天上限"
    return f"ROUND_OVER\tbronze\t{why}\t完成 {st['done']} 放弃 {st['abandoned']} 失败 {st['failed']}"


def build_report(st: dict | None = None) -> str:
    """阶段报告：接了多少、放弃谁、数据量、质量发现、权限发现。

    **放弃要体面**：写清楚是谁、等了多久、随时可重开 ——
    「已知阻塞项」和「失败」是两回事。
    """
    st = st if st is not None else round_state()
    if not st.get("known"):
        return "（读不到任务登记表，无法出报告）"
    if not st.get("started"):
        return "（这一轮还没有任何任务线）"
    # **没结束就别写「结束」。** 早先标题是个三元表达式，两个分支都写
    # 「bronze 轮结束」—— 轮明明还在跑，报告却已经宣布收尾，
    # 而下一行才说「这一轮还没结束，不发」。自相矛盾的报告比没有更糟。
    if not is_over(st):
        return (f"## bronze 轮**还在进行**\n\n"
                f"- 完成 {st['done']} · 放弃 {st['abandoned']} · 失败 {st['failed']}"
                f" · **仍在进行 {st['active']}**\n"
                f"- 历时 {st['days']:.1f} 天（上限 {MAX_DAYS:g} 天）\n\n"
                f"> 还没到终点：既没有全部到终态，也没到天数上限。不出阶段报告。")

    L = [f"## bronze 轮结束（{'全部线到终态' if st['all_terminal'] else f'到达 {MAX_DAYS:g} 天上限'}）",
         "",
         f"- 完成 {st['done']} 条 · 放弃 {st['abandoned']} 条 · 失败 {st['failed']} 条",
         f"- 历时 {st['days']:.1f} 天"]
    if st["active"]:
        L.append(f"- **仍在进行 {st['active']} 条**（到点收轮，它们不算结束）")

    import runs
    ab = runs.by_status("abandoned")
    if ab:
        L += ["", "### 已转为已知阻塞项（随时可重开）"]
        for r in ab[:8]:
            p = r.get("params") or {}
            what = p.get("table") or p.get("asset") or p.get("source") or r["kind"]
            L.append(f"- {r['kind']} · {what} —— {(r.get('note') or '无说明')[:60]}")

    # 数据量与质量发现走各自的账本 —— **这里只汇总，不重算**。
    try:
        from plugins.datasteward_gate.approvals import open_store
        stg = open_store(readonly=True, init_schema=False)
        n_find = stg.db.execute(
            "SELECT count(*) FROM remediation_ledger").fetchone()[0] \
            if hasattr(stg.db, "execute") else 0
        L += ["", f"### 质量与权限发现：{n_find} 处（详见整改台账）"]
    except Exception as e:                                    # noqa: BLE001
        L += ["", f"### 质量与权限发现：（台账不可读：{str(e)[:50]}）"]

    L += ["", "> 这一轮到此为止。**在你决定下一步之前，我不再发起新动作。**"]
    return "\n".join(L)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    st = round_state()
    if "--report" in argv or "--send" in argv:
        body = build_report(st)
        print(body)
        if "--send" in argv:
            if not is_over(st):
                print("\n（这一轮还没结束，不发。）")
                return 0
            # **先落状态，再发信。** 两个理由：
            # ① 发信失败时轮已经关了，静默期照样生效 —— 反过来的话
            #    「发了报告但还在到处发起新动作」，比不发报告更糟；
            # ② 判分要求报告的时刻 **不早于** 轮关闭的时刻
            #    （`score_v2.stage_report_ok` 的 `t >= terminal_at`）。
            #    先发信的话报告永远早于锚点，那一维永远判不过。
            try:
                from plugins.datasteward_gate.approvals import open_store
                with open_store(readonly=False, init_schema=False) as stg:
                    stg.append_event("round", "ROUND_CLOSED",
                                     ("all_terminal" if st.get("all_terminal")
                                      else "deadline"))
            except Exception as e:                            # noqa: BLE001
                print(f"⚠️ ROUND_CLOSED 没落库，静默期不会生效，报告也不发：{e}")
                return 1
            import notify
            n = notify.get()
            to = notify.cfg("MAIL_SPONSOR") or notify.cfg("MAIL_OWNER") or ""
            n.send_notice(to, "[数据管家] 阶段报告：bronze 轮结束", body)
            print(f"\n已通过 {n.name} 发送至 {to}")
        return 0
    line = monitor_line(st)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
