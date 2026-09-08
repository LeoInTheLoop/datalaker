#!/usr/bin/env python3
"""周报（readme 5.5）。

由 cron 每周一发出，**即使无进展也发**——
「本周无进展，因为 3 件事都在等回复」本身就是重要信息。

四段固定结构：本周完成 / 卡在谁那里 / 需要你决策 / 系统开销。
    python3 ops/weekly-report.py            # 打印
    python3 ops/weekly-report.py --send     # 通过 NOTIFY_CHANNEL 发送
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))

from plugins.datasteward_gate.approvals import open_store

WEEK = 7 * 86400


def build():
    st = open_store(readonly=False, init_schema=False)
    since = time.time() - WEEK
    L = []

    # 1. 本周完成
    done = st.completed_since(since)
    L.append("## 本周完成")
    L += [f"- {t} × {n}" for t, n in done] or ["- （无）"]

    # 2. 卡在谁那里
    stale = [r for r in st.stale_items() if float(r[5]) >= 24]
    L.append("\n## 卡在谁那里")
    if stale:
        for _id, who, tool, kind, lvl, age_h in stale[:8]:
            real = st.resolve_role(who) or who
            tag = ["", "已催办", "已升级 Owner", "已报 Sponsor"][min(lvl, 3)]
            L.append(f"- **{real}** · {tool} · 等待 {age_h/24:.0f} 天"
                     + (f"（{tag}）" if tag else ""))
    else:
        L.append("- （无阻塞）")

    ab = st.abandoned()
    if ab:
        L.append(f"\n### 已转为已知阻塞项（{len(ab)} 件）")
        L += [f"- {t} · 原负责 {w}" for _i, w, t in ab[:5]]
        L.append("> 这些已退出活跃队列，不再消耗额度。需要时可手动恢复。")

    # 3. 需要你决策
    pend = [r for r in st.stale_items() if float(r[5]) < 24]
    L.append(f"\n## 需要你决策（{len(pend)} 件）")
    L += [f"- {tool} · {st.resolve_role(who) or who}"
          for _i, who, tool, _k, _l, _a in pend[:8]] or ["- （无）"]

    # 4. 系统开销
    L.append("\n## 系统开销")
    try:
        q, u = st.ledger_summary(since)
        L.append(f"- 源系统查询 {q[0]} 次，返回 {q[1]:,} 行，拒绝 {q[2] or 0} 次")
        L.append(f"- 模型调用 {u[0]} 次，{u[1]:,} tokens，${u[2]:.4f}")
        L.append("\n> 「拒绝」是护栏生效，不是故障。")
    except Exception as e:
        L.append(f"- （账本不可读：{str(e)[:60]}）")

    return "\n".join(L)


if __name__ == "__main__":
    body = build()
    print(body)
    if "--send" in sys.argv:
        import notify
        n = notify.get()
        to = notify.cfg("MAIL_SPONSOR") or notify.cfg("MAIL_OWNER") or ""
        res = n.send_notice(to, f"[数据管家] 周报 {time.strftime('%m-%d')}", body)
        print(f"\n通道 {n.name}：{notify.sent_line(res, to)}")
