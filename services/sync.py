"""增量同步与新鲜度（readme 6.1）。

> **一次性抽取的 lake，从落地那天起就在过期。**

两种过期方式，危害不同：**漏了**（新行没同步，明显）vs
**错了**（旧值还在，查询照常返回结果只是返回的是错的，隐蔽得多）。

真正写入 bronze —— 经 Trino 以 `claw` 身份操作 Iceberg（R3 授权已生效）。
"""
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TRINO_C = os.environ.get("TRINO_CONTAINER", "datalaker-trino-1")
TRINO_USER = os.environ.get("TRINO_WRITE_USER", "claw")
BRONZE = "iceberg.bronze"
# source_id -> Trino catalog。历史原因 olist 的 catalog 名叫 postgres
CATALOG = {"olist": "postgres", "northwind": "northwind"}


class SyncError(RuntimeError):
    pass


class SchemaDrift(SyncError):
    """源系统 schema 变了。**必须停下来问人**（readme 6.1：L2）。

    新增字段是什么意思，属于业务知识而非技术知识——
    Agent 不应自行决定要不要、以及怎么用。
    """


def _trino(sql: str, timeout=180):
    r = subprocess.run(
        ["docker", "exec", TRINO_C, "trino", "--user", TRINO_USER,
         "--output-format", "CSV_UNQUOTED", "--execute", sql],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SyncError((r.stderr or r.stdout).strip()[:300])
    return [l for l in r.stdout.strip().split("\n") if l]


def _store(readonly=False):
    from plugins.datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=False)


def _schema_hash(cols) -> str:
    """列名 + 类型的指纹。变了就是 schema 漂移。"""
    return hashlib.sha256(
        json.dumps([[c["name"], c["type"]] for c in cols], sort_keys=True).encode()
    ).hexdigest()[:16]


def get_state(asset: str) -> dict | None:
    st = _store(readonly=True)
    try:
        row = st.db.execute(
            "SELECT strategy, watermark, last_synced_at, data_as_of,"
            " freshness_sla_h, schema_hash, row_count FROM sync_state WHERE asset=?",
            (asset,)).fetchone() if hasattr(st.db, "execute") else None
    except Exception:
        row = None
    if row is None:
        try:
            with st.db.cursor() as c:
                c.execute("SELECT strategy, watermark, last_synced_at, data_as_of,"
                          " freshness_sla_h, schema_hash, row_count"
                          " FROM sync_state WHERE asset=%s", (asset,))
                row = c.fetchone()
        except Exception:
            return None
    if not row:
        return None
    keys = ("strategy", "watermark", "last_synced_at", "data_as_of",
            "freshness_sla_h", "schema_hash", "row_count")
    return dict(zip(keys, row))


def save_state(asset, strategy, watermark=None, schema_hash=None, row_count=None,
               sla_h=24, error=None):
    st = _store()
    with st.db.cursor() as c:
        c.execute(
            "INSERT INTO sync_state (asset, strategy, watermark, last_synced_at,"
            " data_as_of, freshness_sla_h, schema_hash, row_count, last_error)"
            " VALUES (%s,%s,%s, now(), now(), %s,%s,%s,%s)"
            " ON CONFLICT (asset) DO UPDATE SET strategy=excluded.strategy,"
            " watermark=excluded.watermark, last_synced_at=now(),"
            " data_as_of=now(), schema_hash=excluded.schema_hash,"
            " row_count=excluded.row_count, last_error=excluded.last_error",
            (asset, strategy, watermark, sla_h, schema_hash, row_count, error))


def freshness(asset: str) -> dict:
    """新鲜度必须可见（readme 6.1）。超 SLA 不静默降级，显式告警进周报。"""
    s = get_state(asset)
    if not s:
        return {"asset": asset, "known": False}
    last = s["last_synced_at"]
    age_h = None
    if last is not None:
        try:
            age_h = (time.time() - last.timestamp()) / 3600
        except AttributeError:
            age_h = (time.time() - float(last)) / 3600
    return {"asset": asset, "known": True, "strategy": s["strategy"],
            "age_hours": round(age_h, 2) if age_h is not None else None,
            "sla_hours": s["freshness_sla_h"],
            "is_stale": bool(age_h is not None and age_h > s["freshness_sla_h"]),
            "row_count": s["row_count"]}


def _pg_type_to_trino(t: str) -> str:
    t = (t or "").lower()
    if "int" in t and "point" not in t:
        return "bigint"
    if any(k in t for k in ("numeric", "decimal", "real", "double")):
        return "double"
    if "bool" in t:
        return "boolean"
    if "timestamp" in t:
        return "timestamp(6)"
    if t == "date":
        return "date"
    return "varchar"


def sync_table(source_id: str, table: str, watermark_col: str | None = None,
               sla_h: int = 24, allow_schema_change: bool = False) -> dict:
    """同步一张表到 bronze。

    策略由是否有可用水位列决定：有则增量追加，无则全量刷新。
    **schema 漂移会中止同步并要求人确认**——不是自动接受。
    """
    import data_tools
    asset = f"{source_id}.{table}"
    meta = data_tools.get_table_metadata(source_id, table)
    sh = _schema_hash(meta["columns"])
    prev = get_state(asset)

    if prev and prev["schema_hash"] and prev["schema_hash"] != sh and not allow_schema_change:
        raise SchemaDrift(
            f"{asset} 的 schema 已变化（{prev['schema_hash']} → {sh}）。"
            f"新增/变更字段的业务含义需要人确认，同步已暂停。")

    _trino(f"CREATE SCHEMA IF NOT EXISTS {BRONZE}")
    cols_ddl = ", ".join(f'"{c["name"]}" {_pg_type_to_trino(c["type"])}'
                         for c in meta["columns"])
    tgt = f'{BRONZE}."{source_id}__{table}"'

    strategy = "incremental_append" if watermark_col else "full_refresh"
    if strategy == "full_refresh":
        _trino(f"DROP TABLE IF EXISTS {tgt}")
        _trino(f"CREATE TABLE {tgt} ({cols_ddl})")
        where = ""
    else:
        _trino(f"CREATE TABLE IF NOT EXISTS {tgt} ({cols_ddl})")
        wm = prev["watermark"] if prev else None
        where = f" WHERE \"{watermark_col}\" > TIMESTAMP '{wm}'" if wm else ""

    collist = ", ".join(f'"{c["name"]}"' for c in meta["columns"])
    src = f'{CATALOG.get(source_id, source_id)}.public."{table}"'
    _trino(f"INSERT INTO {tgt} ({collist}) SELECT {collist} FROM {src}{where}")

    n = int(_trino(f"SELECT count(*) FROM {tgt}")[0])
    new_wm = None
    if watermark_col:
        r = _trino(f'SELECT max("{watermark_col}") FROM {tgt}')
        new_wm = r[0] if r and r[0] else None

    save_state(asset, strategy, watermark=new_wm, schema_hash=sh,
               row_count=n, sla_h=sla_h)
    return {"asset": asset, "strategy": strategy, "bronze_table": tgt,
            "row_count": n, "watermark": new_wm}


def reconcile_deletes(source_id: str, table: str, pk: str) -> dict:
    """硬删除对账（readme 6.1 四个坑之一）。

    增量同步看不到源系统的物理删除，lake 里会留下幽灵数据。
    定期比对主键集合是唯一可靠的办法。
    """
    import data_tools
    _ = data_tools  # 保持依赖显式
    tgt = f'{BRONZE}."{source_id}__{table}"'
    src = f'{CATALOG.get(source_id, source_id)}.public."{table}"'
    rows = _trino(
        f'SELECT count(*) FROM (SELECT "{pk}" FROM {tgt} '
        f'EXCEPT SELECT "{pk}" FROM {src}) g')
    ghosts = int(rows[0]) if rows else 0
    return {"asset": f"{source_id}.{table}", "ghost_rows": ghosts,
            "note": "源系统已删除但 bronze 仍存在的行" if ghosts else "无幽灵数据"}
