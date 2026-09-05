#!/usr/bin/env python3
"""资产台账：查、导出、回填。

台账（`asset_provenance`）由 `post_tool_call` 在动作发生时自动写，
**append-only**。这个脚本是它的外部入口 —— 给人、给审计、给别的系统。

    python3 ops/provenance.py                     列出所有有档的资产
    python3 ops/provenance.py acme.fin_invoice    一张表的来历
    python3 ops/provenance.py --export out.json   导出全部（喂给审计工具）
    python3 ops/provenance.py --backfill          从既有记录反推历史

## 关于回填

加台账之前接进来的表，台账里是空的。回填能从 `source_grants` /
`approvals+decisions` / `sync_state` / `asset_semantics` 反推出来 ——
**但反推不等于当时记的**：时间可能只精确到「那次同步」，动作之间的
先后可能丢失，中途失败又重试的痕迹一定丢。所以每条回填记录都带
`backfilled: true`，查询时原样显示。

**审计问「这是当时记的还是后来补的」，必须答得出来。**
"""
import argparse
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

VERB = {"source_registered": "源接入", "ingested": "入湖",
        "ingested_from_file": "从文件入湖", "cleaned": "清洗",
        "published": "发布 gold", "granted": "开读权限",
        "semantics_defined": "定口径", "refreshed": "全量刷新"}


def _store(write=False):
    from datasteward_gate.approvals import open_store
    return open_store(readonly=not write, init_schema=True)


def _ts(v) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v)))
    except Exception:                                         # noqa: BLE001
        return str(v)


def show(asset=None) -> int:
    with _store() as st:
        rows = st.provenance(asset, limit=1000)
    if not rows:
        print(f"\n（{asset or '台账'}无记录）"
              f"\n湖里若有数据而台账为空，那是缺口本身 —— 可以 --backfill 反推，"
              f"\n但反推出来的东西会被标成 backfilled，别当成当时记的。\n")
        return 1
    if asset:
        print(f"\n# {asset} 的来历\n")
        for r in rows:
            d = r["detail"] or {}
            tag = " ⟨回填⟩" if d.get("backfilled") else ""
            extra = (d.get("connection_identity") or d.get("path")
                     or ("规则 " + "、".join(d["approved_rules"])
                         if d.get("approved_rules") else "")
                     or (d.get("result") or "")[:50])
            print(f"  {_ts(r['ts'])}  {VERB.get(r['event'], r['event'])}{tag}"
                  f"{'：' + str(extra) if extra else ''}")
            print(f"      谁 {r['actor'] or '—'} · 依据 "
                  f"{(r['approval_id'] or '—')[:8]}")
    else:
        by_asset = {}
        for r in rows:
            by_asset.setdefault(r["asset"], []).append(r)
        print(f"\n台账里有 {len(by_asset)} 个资产：\n")
        for a, rs in sorted(by_asset.items()):
            ev = "→".join(VERB.get(x["event"], x["event"]) for x in rs)
            bf = " ⟨含回填⟩" if any((x["detail"] or {}).get("backfilled")
                                    for x in rs) else ""
            print(f"  {a:<30} {ev}{bf}")
    print()
    return 0


def export(path: str) -> int:
    with _store() as st:
        rows = st.provenance(limit=100000)
    pathlib.Path(path).write_text(
        json.dumps({"exported_at": time.time(), "count": len(rows),
                    "records": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"导出 {len(rows)} 条 → {path}")
    return 0


def backfill() -> int:
    """从既有记录反推历史。**只补台账里没有的资产**，不覆盖已有的。

    反推得到的东西比当时记的粗：时间取那次同步/决定的时刻，
    动作之间的先后按时间排，失败重试的痕迹一律丢失。
    每条都标 `backfilled: true`。
    """
    added = 0
    with _store(write=True) as st:
        q = st.db.execute
        have = {r["asset"] for r in st.provenance(limit=100000)}

        # 1) 源接入：谁给的、什么账号、依据哪份审批
        for sid, by, at, aid, dsn in q(
                "SELECT s.source_id, s.registered_by, s.registered_at,"
                " s.approval_id, s.dsn FROM source_secrets s"):
            if sid in have:
                continue
            ident = ""
            try:
                rest = str(dsn).split("://", 1)[1]
                cred, host = rest.split("@", 1)
                ident = f"{cred.split(':', 1)[0]}@{host}"
            except Exception:                                 # noqa: BLE001
                pass
            st.record_provenance(sid, "source_registered", actor=by,
                                 approval_id=aid,
                                 detail={"backfilled": True,
                                         "connection_identity": ident})
            added += 1

        # 2) 入湖：sync_state 是同步这件事自己的记录，比反查表名可靠
        for asset, ts, n, strat in q(
                "SELECT asset, last_synced_at, row_count, strategy"
                " FROM sync_state WHERE last_synced_at IS NOT NULL"):
            if asset in have:
                continue
            who = ""
            try:
                tbl = asset.split(".", 1)[1]
                row = q("SELECT d.approver FROM approvals a JOIN decisions d"
                        " ON d.approval_id=a.id WHERE a.tool_name='ingest_table'"
                        " AND a.args_json LIKE ? ORDER BY d.decided_at DESC"
                        " LIMIT 1", (f'%"{tbl}"%',)).fetchone()
                who = row[0] if row else ""
            except Exception:                                 # noqa: BLE001
                pass
            st.record_provenance(asset, "ingested", actor=who,
                                 detail={"backfilled": True, "rows": n,
                                         "strategy": strat})
            added += 1

        # 3) 口径：谁定的、什么时候
        for a, k, by, at in q("SELECT asset, key, confirmed_by, confirmed_at"
                              " FROM asset_semantics"):
            base = ".".join(a.split(".")[:2])
            if base in have:
                continue
            st.record_provenance(a, "semantics_defined", actor=by,
                                 detail={"backfilled": True, "key": k})
            added += 1

    print(f"回填 {added} 条 —— **都带 backfilled 标记**，"
          f"审计问「当时记的还是后来补的」答得出来。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="资产台账")
    ap.add_argument("asset", nargs="?", help="资产名，如 acme.fin_invoice")
    ap.add_argument("--export", metavar="PATH", help="导出全部为 JSON")
    ap.add_argument("--backfill", action="store_true", help="从既有记录反推历史")
    a = ap.parse_args(argv)
    if a.backfill:
        return backfill()
    if a.export:
        return export(a.export)
    return show(a.asset)


if __name__ == "__main__":
    raise SystemExit(main())
