"""时间引擎 —— 让「等 8 小时 / 等 1 天 / 12 天后放弃」在几秒内跑完。

**时间当数据处理，不当代码改。**

升级链路读的是 `now() - approvals.created_at`（见 `ops/escalate.py` 的
`stale_items`），所以把 `created_at` 往前推 N 天，与真的等了 N 天**完全等价**——
被测代码一行不用改，也就不存在「测试专用分支」这种东西。

考虑过的另一条路是给被测代码注入可控时钟（`CLAW_NOW_OFFSET_DAYS`），
放弃了：升级判断发生在 SQL 里的 `now()`，进程侧的偏移量根本影响不到它，
真要做就得把所有时间读取抽象一层——为测试改产品结构，代价方向反了。

Agent 对 `approvals` 只有受限写权限（机制三），因此**只有 eval 侧能回拨时间**，
这本身也是一层保护：Agent 无法通过改时间戳绕过升级或过期。

    python3 evals/clock.py --advance 6 --db /tmp/x.db
"""
import argparse
import pathlib
import sqlite3
import sys

DAY = 86400.0

# 表 → (时间列, 是否只动未决行)
TARGETS = {
    "approvals": (["created_at", "expires_at"], True),
    "runs": (["created_at", "updated_at"], True),
}


def _is_sqlite(db_path):
    return bool(db_path)


def _id_clause(only_ids, ph="?"):
    """只推指定的几条。

    大 case 里 10 个人不会同时回信 —— 有人当天回、有人拖到第 9 天、
    有人根本不回。全表一起推只能模拟「所有人一样慢」，那不是真实情形。
    """
    if not only_ids:
        return "", ()
    return f" AND id IN ({','.join([ph] * len(only_ids))})", tuple(only_ids)


def advance_sqlite(db_path: str, days: float, only_pending=True,
                   only_ids=None) -> dict:
    """SQLite：时间列是 REAL（unix 秒），直接减。"""
    con = sqlite3.connect(db_path)
    out = {}
    delta = days * DAY
    try:
        for tbl, (cols, pend) in TARGETS.items():
            if not con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (tbl,)).fetchone():
                continue
            where, args = "", ()
            if only_pending and pend and tbl == "approvals":
                # 只推未决的 —— 已决的历史不该被篡改，那是审计轨
                where = (" WHERE id NOT IN (SELECT approval_id FROM decisions)"
                         " AND abandoned_at IS NULL")
                extra, args = _id_clause(only_ids)
                where += extra
            elif only_pending and pend and tbl == "runs":
                where = " WHERE status='waiting_human'"
            sets = ", ".join(f"{c}={c}-{delta}" for c in cols)
            cur = con.execute(f"UPDATE {tbl} SET {sets}{where}", args)
            out[tbl] = cur.rowcount
        con.commit()
    finally:
        con.close()
    return out


def advance_pg(dsn: str, days: float, only_pending=True, only_ids=None) -> dict:
    """Postgres：时间列是 TIMESTAMPTZ，减 interval。"""
    import psycopg
    out = {}
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as c:
            for tbl, (cols, pend) in TARGETS.items():
                c.execute("SELECT to_regclass(%s)", (tbl,))
                if not c.fetchone()[0]:
                    continue
                where, args = "", ()
                if only_pending and pend and tbl == "approvals":
                    where = (" WHERE id NOT IN (SELECT approval_id FROM decisions)"
                             " AND abandoned_at IS NULL")
                    extra, args = _id_clause(only_ids, "%s")
                    where += extra
                elif only_pending and pend and tbl == "runs":
                    where = " WHERE status='waiting_human'"
                if tbl == "runs":     # runs 的时间列是 double precision
                    sets = ", ".join(f"{col}={col}-{days * DAY}" for col in cols)
                else:
                    sets = ", ".join(
                        f"{col}={col} - interval '{days} days'" for col in cols)
                c.execute(f"UPDATE {tbl} SET {sets}{where}", args)
                out[tbl] = c.rowcount
        conn.commit()
    return out


def advance(days: float, db: str | None = None, dsn: str | None = None,
            only_pending: bool = True, only_ids=None) -> dict:
    """把「现在」往后推 days 天（实现上是把未决事项的时间戳往前推）。

    `only_ids` 只推指定的审批 —— 用来模拟「有人当天回、有人拖到第 9 天」。
    """
    if dsn:
        return advance_pg(dsn, days, only_pending, only_ids)
    if not db:
        raise ValueError("需要 db（SQLite 路径）或 dsn（Postgres）")
    return advance_sqlite(db, days, only_pending, only_ids)


def ages(db: str) -> list:
    """未决事项各等了多久（天）。用来断言升级确实按阶梯走。"""
    import time
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT a.id, a.approver, a.escalation_level,"
            " (?-a.created_at)/86400.0 FROM approvals a"
            " LEFT JOIN decisions d ON d.approval_id=a.id"
            " WHERE d.id IS NULL AND a.abandoned_at IS NULL"
            " ORDER BY a.created_at", (time.time(),)).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "approver": r[1], "level": r[2], "age_days": round(r[3], 2)}
            for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description="把未决事项的时钟往前推")
    ap.add_argument("--advance", type=float, required=True, help="推进多少天")
    ap.add_argument("--db"), ap.add_argument("--dsn")
    ap.add_argument("--all", action="store_true", help="连已决的一起推（一般不要）")
    a = ap.parse_args(argv)
    r = advance(a.advance, a.db, a.dsn, only_pending=not a.all)
    print(f"时钟推进 {a.advance} 天：" + ", ".join(f"{k} {v} 行" for k, v in r.items()))
    if a.db:
        for x in ages(a.db):
            print(f"  {x['id'][:8]}  {x['approver']:<12} L{x['level']}  "
                  f"{x['age_days']:.1f} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
