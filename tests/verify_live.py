#!/usr/bin/env python3
"""一次演练跑完之后，**独立查库**看到底发生了什么。

不看 Agent 的说法、不看日志里的「成功」—— 自己连 Trino 和治理库数一遍。
这个项目摔过的第 1 号坑就是「验收看状态栏，而 lake 里是空的」，
而第二次是「bronze 里有 600 行，但那是上一轮回归留下的」。
所以每一项都带**出处**：行数从哪查的、时刻是谁写的。

    DATASTEWARD_DB=/tmp/live.db python3 tests/verify_live.py
    python3 tests/verify_live.py --json     # 机器读
"""
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

# 历史 eval 跑留下的表：名字里带 eval_ 的都不是这次演练的产物。
# 不排掉的话「bronze 里有数据」永远成立 —— 那正是第二次踩的坑。
EVAL_PREFIXES = ("eval_",)


def _is_eval(table: str) -> bool:
    return any(p in table for p in EVAL_PREFIXES)


def lake_state() -> dict:
    """bronze / silver 里有什么。**独立连 Trino**，不经过被测代码的说法。"""
    out = {"bronze": {}, "silver": {}, "error": ""}
    try:
        import sync
        for schema in ("bronze", "silver"):
            tabs = [t for t in sync._trino(
                "SELECT table_name FROM iceberg.information_schema.tables"
                f" WHERE table_schema='{schema}'") if not _is_eval(t)]
            for t in sorted(tabs):
                try:
                    out[schema][t] = int(sync._trino(
                        f'SELECT count(*) FROM iceberg.{schema}."{t}"')[0])
                except Exception as e:                        # noqa: BLE001
                    out[schema][t] = f"查不到（{type(e).__name__}）"
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def gov_state() -> dict:
    """治理库：源、凭证、线、口径、审批、轮。"""
    out = {}
    try:
        from datasteward_gate.approvals import open_store
        import runs
        with open_store(readonly=False, init_schema=False) as st:
            q = st.db.execute
            out["granted_sources"] = sorted(st.granted_sources())
            out["secrets"] = sorted(r[0] for r in q(
                "SELECT source_id FROM source_secrets"))
            out["semantics"] = [
                {"asset": a, "key": k, "value": v[:60], "by": c}
                for a, k, v, c in q(
                    "SELECT asset, key, value, confirmed_by FROM asset_semantics")]
            out["approvals"] = {
                "total": q("SELECT count(*) FROM approvals").fetchone()[0],
                "decided": q("SELECT count(*) FROM approvals a JOIN decisions d"
                             " ON d.approval_id=a.id").fetchone()[0],
                "by_tool": dict(q("SELECT tool_name, count(*) FROM approvals"
                                  " GROUP BY tool_name").fetchall()),
            }
            out["silver_round_open"] = st.stage_choice("start_silver") is not None
            out["round_closed"] = st.round_closed_at() is not None
            out["blocked"] = dict(q(
                "SELECT kind, count(*) FROM events WHERE kind LIKE 'BLOCKED_%'"
                " GROUP BY kind").fetchall())
            out["sync_state"] = [
                {"asset": a, "rows": n,
                 "synced": time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"}
                for a, ts, n in q(
                    "SELECT asset, last_synced_at, row_count FROM sync_state"
                    " ORDER BY asset")]
        out["runs"] = {s: len(runs.by_status(s)) for s in
                       ("running", "waiting_human", "done", "abandoned", "failed")}
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def claimed_vs_actual(gov: dict, lake: dict) -> list:
    """**工具说的行数** vs **独立查库的行数**，逐张对。

    对不上就是问题：要么没真写，要么写了别的地方。
    """
    rows = []
    for rec in gov.get("sync_state", []):
        asset = rec["asset"]
        if "." not in asset:
            continue
        src, tbl = asset.split(".", 1)
        name = f"{src}__{tbl}"
        actual = lake["bronze"].get(name)
        rows.append({"table": name, "claimed": rec["rows"], "actual": actual,
                     "ok": actual == rec["rows"]})
    return rows


def mail_state() -> dict:
    """谁收到了什么。**看收件箱，不看 outbox 日志。**"""
    out = {}
    try:
        sys.path.insert(0, str(ROOT / "tests"))
        import mailsim
        if not mailsim.probe():
            return {"error": "GreenMail 未启动"}
        for box in ("boss@acme.com", "wang@acme.com", "zhou@acme.com",
                    "sun@acme.com", "dba@acme.com", "it@acme.com"):
            subs = [mailsim.subject_of(m) for m in mailsim.fetch(box)]
            if subs:
                out[box] = {"count": len(subs),
                            "approvals": sum(1 for s in subs if "请批准" in s),
                            "last": subs[-1][:50]}
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


def report() -> dict:
    lake = lake_state()
    gov = gov_state()
    return {"lake": lake, "gov": gov, "mail": mail_state(),
            "cross_check": claimed_vs_actual(gov, lake)}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    r = report()
    if "--json" in argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    g, l = r["gov"], r["lake"]
    print(f"\n=== 治理库（{os.environ.get('DATASTEWARD_DB', '?')}）===\n")
    if g.get("error"):
        print("  读不到：", g["error"])
    else:
        print(f"  已接入的源   {g['granted_sources']}")
        print(f"  凭证已存放   {g['secrets']}")
        print(f"  任务线       {g['runs']}")
        print(f"  审批         {g['approvals']['total']} 份，"
              f"已决 {g['approvals']['decided']} · {g['approvals']['by_tool']}")
        print(f"  清洗轮       {'已开' if g['silver_round_open'] else '未开'}"
              f" · 轮结束 {'是' if g['round_closed'] else '否'}")
        print(f"  门禁拦截     {g['blocked'] or '（无）'}")
        print(f"\n  口径 {len(g['semantics'])} 条：")
        for s in g["semantics"]:
            print(f"    {s['asset']} [{s['key']}] = {s['value']}（{s['by']}）")

    print(f"\n=== 数据湖（独立连 Trino 查，已排除历史 eval 表）===\n")
    if l.get("error"):
        print("  读不到：", l["error"])
    else:
        for schema in ("bronze", "silver"):
            print(f"  {schema}：{len(l[schema])} 张")
            for t, n in l[schema].items():
                print(f"    {t:<34} {n if isinstance(n, str) else f'{n:,}'} 行")

    print(f"\n=== 交叉核对：工具说的 vs 独立查库 ===\n")
    bad = [x for x in r["cross_check"] if not x["ok"]]
    for x in r["cross_check"]:
        mark = "✅" if x["ok"] else "❌"
        print(f"  {mark} {x['table']:<32} 说 {x['claimed']} / 实 {x['actual']}")
    if not r["cross_check"]:
        print("  （还没有同步记录）")

    print(f"\n=== 收件箱 ===\n")
    for box, d in r["mail"].items():
        if box == "error":
            print("  ", d)
            continue
        print(f"  {box:<18} {d['count']:>3} 封（审批 {d['approvals']}）"
              f" 最新：{d['last']}")

    print()
    if bad:
        print(f"❌ {len(bad)} 张表对不上 —— 工具说写了，独立查库没有")
        return 1
    print("✅ 工具说的与独立查库一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
