#!/usr/bin/env python3
"""超时逐级升级 → 优雅放弃（readme 5.4）。

由 cron 每小时调用，**独立于 Agent 运行**——Agent 挂了催办也不能停。

    T+3d   催办（同一人）
    T+6d   升级 Owner，抄送原收件人
    T+9d   升级 Sponsor
    T+12d  标记 ABANDONED —— 不是失败，是转为「已知阻塞项」

放弃≠删除：退出活跃队列不再消耗 WIP 额度，event log 完整保留，
人想起来后随时可手动 resume。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))

from plugins.datasteward_gate.approvals import open_store

LADDER = [(3, 1, "催办"), (6, 2, "升级 Owner"), (9, 3, "升级 Sponsor")]
ABANDON_DAYS = int(os.environ.get("ABANDON_AFTER_DAYS", "12"))


def main(dry_run=False):
    st = open_store(readonly=False, init_schema=False)
    acted = []
    for item_id, approver, tool, kind, level, age_h in st.stale_items():
        age_d = float(age_h) / 24.0   # Postgres 返回 Decimal

        if age_d >= ABANDON_DAYS:
            if not dry_run:
                st.abandon(item_id)
                st.append_event(item_id, "ABANDONED",
                                f"{approver} 超过 {ABANDON_DAYS} 天未响应")
            acted.append(("ABANDONED", item_id[:8], approver, tool, f"{age_d:.1f}d"))
            continue

        # 一次升到应到的层级：停机数天后重启，不该还一小时升一级慢慢追
        target, label = 0, ""
        for days, lvl, lb in LADDER:
            if age_d >= days:
                target, label = lvl, lb
        if target > level:
            for _ in (1,):
                lvl = target
                if not dry_run:
                    st.bump_escalation(item_id, lvl)
                    st.append_event(item_id, f"ESCALATED_{lvl}", f"{label} · {approver}")
                    try:
                        import notify
                        import mail_threads
                        to = st.resolve_role(approver) or approver
                        # 催办是人最可能**直接回信**的一封（「这个不归我管」
                        # 「换个账号试试」）。不带 token 的话，一个人手上同时
                        # 有两条线时，他回的是哪一条完全无从判断。
                        notify.get().send_notice(
                            to, f"[数据管家] {label}：{tool}",
                            f"这件事已等待 {age_d:.0f} 天未获回应。\n"
                            f"若不属于你的职责范围，请回复告知应当找谁。\n"
                            f"第 {ABANDON_DAYS} 天仍无回应时，我会将其转为已知阻塞项"
                            f"并在周报中说明，同时继续推进其他任务线。",
                            run_id=mail_threads.run_of_approval(item_id))
                    except Exception:
                        pass
                acted.append((f"ESCALATE_{lvl}", item_id[:8], approver, tool, f"{age_d:.1f}d"))

    if acted:
        print(f"{'[dry-run] ' if dry_run else ''}处理 {len(acted)} 项：")
        for a in acted:
            print(f"  {a[0]:<12} {a[1]}  {a[2]:<12} {a[3]:<18} 等待 {a[4]}")
    else:
        print("无需升级的事项")
    return len(acted)


if __name__ == "__main__":
    sys.exit(0 if main("--dry-run" in sys.argv) >= 0 else 1)
