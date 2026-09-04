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
# source_id → Trino catalog。默认同名，这里只登记不同名的那些。
# 新增一个源 = 加一个 catalog 文件 + 必要时在这里加一行。
CATALOG = {"olist": "postgres"}


class SyncError(RuntimeError):
    pass


class SchemaDrift(SyncError):
    """源系统 schema 变了。**必须停下来问人**（readme 6.1：L2）。

    新增字段是什么意思，属于业务知识而非技术知识——
    Agent 不应自行决定要不要、以及怎么用。
    """


TRINO_URL = os.environ.get("TRINO_URL", "")
def _envfile():
    import pathlib
    d, f = {}, pathlib.Path(ROOT) / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


_E = _envfile()
# 默认值与 password.db 里的开发口令一致；生产必须改（11.6）
TRINO_PASSWORD = (os.environ.get("TRINO_PASSWORD")
                  or _E.get("TRINO_PASSWORD") or "claw_pw_change_me")
TRINO_SERVER = os.environ.get("TRINO_SERVER", "https://localhost:8443")


def _trino_http(sql: str, timeout=180):
    """走 Trino 的 REST 协议。

    **容器里没有 docker CLI。** 早先只有 `docker exec` 一条路，
    意味着 Agent 一旦真的跑在受限容器里就写不了 bronze —— 这个洞
    只有把 Agent 塞进容器才会暴露出来，在宿主机上跑一万遍都发现不了。

    身份走用户名 + 密码，因此 `rules.json` 的授权对它同样生效：
    容器用 claw 的凭证，就只能读源、只能写 iceberg。
    """
    import base64
    import urllib.request

    url = TRINO_URL.rstrip("/") + "/v1/statement"
    hdr = {"X-Trino-User": TRINO_USER, "Content-Type": "text/plain"}
    if TRINO_PASSWORD:
        tok = base64.b64encode(
            f"{TRINO_USER}:{TRINO_PASSWORD}".encode()).decode()
        hdr["Authorization"] = f"Basic {tok}"

    rows, err = [], None
    req = urllib.request.Request(url, data=sql.encode("utf-8"),
                                 headers=hdr, method="POST")
    while True:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        if body.get("error"):
            err = body["error"].get("message", "unknown")
            break
        for row in body.get("data") or []:
            rows.append(",".join("" if v is None else str(v) for v in row))
        nxt = body.get("nextUri")
        if not nxt:
            break
        req = urllib.request.Request(nxt, headers=hdr)
    if err:
        raise SyncError(err[:300])
    return rows


def _trino_exec(sql: str, timeout=180):
    """宿主机开发路径：docker exec 调 CLI。

    **也要带凭证。** 早先它连的是免认证的 8080 —— 那个免认证正是
    「网络内谁都能冒充 admin」这个洞的来源。堵掉洞之后这条路必须
    跟别人一样报身份，否则等于给开发留了后门。
    """
    cmd = ["docker", "exec"]
    if TRINO_PASSWORD:
        cmd += ["-e", f"TRINO_PASSWORD={TRINO_PASSWORD}"]
    cmd += [TRINO_C, "trino", "--server", TRINO_SERVER, "--user", TRINO_USER,
            "--output-format", "CSV_UNQUOTED", "--execute", sql]
    if TRINO_PASSWORD:
        cmd += ["--password", "--insecure"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SyncError((r.stderr or r.stdout).strip()[:300])
    return [l for l in r.stdout.strip().split("\n")
            if l and "JAVA_TOOL_OPTIONS" not in l]

def _trino(sql: str, timeout=180):
    """配了 TRINO_URL 就走 HTTP（容器内唯一可行），否则 docker exec。"""
    return (_trino_http(sql, timeout) if TRINO_URL
            else _trino_exec(sql, timeout))

def _store(readonly=False):
    from plugins.datasteward_gate.approvals import open_store
    # 写入侧要保证 schema 在（DDL 全是 IF NOT EXISTS，幂等且便宜）。
    # 老库里没有 sync_state，不在这里补就永远缺一张表。
    return open_store(readonly=readonly, init_schema=not readonly)


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
    """落同步状态。

    两个后端的 upsert 语法与时间函数都不同，`get_state` 早就分了岔，
    这里原先只写了 Postgres 一路 —— 本地默认是 SQLite，于是整条同步路径不通。
    """
    st = _store()
    if hasattr(st.db, "execute") and not hasattr(st.db, "cursor_factory"):
        now = time.time()
        st.db.execute(
            "INSERT INTO sync_state (asset, strategy, watermark, last_synced_at,"
            " data_as_of, freshness_sla_h, schema_hash, row_count, last_error)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(asset) DO UPDATE SET strategy=excluded.strategy,"
            " watermark=excluded.watermark, last_synced_at=excluded.last_synced_at,"
            " data_as_of=excluded.data_as_of, schema_hash=excluded.schema_hash,"
            " row_count=excluded.row_count, last_error=excluded.last_error",
            (asset, strategy, watermark, now, now, sla_h, schema_hash,
             row_count, error))
        st.db.commit()
        return
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


def lake_columns(schema: str, table: str) -> list:
    """lake 里某张表的列名（按序）。bronze/silver/gold 都走这一个。

    清洗和发布都要先知道有哪些列；各写一份 information_schema 查询，
    早晚在 `_raw` 列该不该带上这种问题上分叉。
    """
    rows = _trino(
        "SELECT column_name FROM iceberg.information_schema.columns"
        f" WHERE table_schema='{schema}' AND table_name='{table}'"
        " ORDER BY ordinal_position")
    return [r.strip().strip('"') for r in rows if r.strip()]


def sync_table(source_id: str, table: str, watermark_col: str | None = None,
               sla_h: int = 24, allow_schema_change: bool = False,
               schema: str = "public", bronze_name: str | None = None) -> dict:
    """同步一张表到 bronze。

    策略由是否有可用水位列决定：有则增量追加，无则全量刷新。
    **schema 漂移会中止同步并要求人确认**——不是自动接受。
    """
    import data_tools
    asset = f"{source_id}.{table}" if schema == "public" else f"{source_id}.{schema}.{table}"
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
    tgt = (f'{BRONZE}."{bronze_name}"' if bronze_name else
           f'{BRONZE}."{source_id}__{table}"' if schema == "public"
           else f'{BRONZE}."{source_id}__{schema}__{table}"')

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
    # schema 可指定 —— eval 把脏副本放在 eval_<case> 而不是 public，
    # 写死 public 会让整条 eval 读不到自己刚布置的考场。
    src = f'{CATALOG.get(source_id, source_id)}."{schema}"."{table}"'
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


def reconcile_deletes(source_id: str, table: str, pk: str,
                      schema: str = "public") -> dict:
    """硬删除对账（readme 6.1 四个坑之一）。

    增量同步看不到源系统的物理删除，lake 里会留下幽灵数据。
    定期比对主键集合是唯一可靠的办法。
    """
    import data_tools
    _ = data_tools  # 保持依赖显式
    tgt = (f'{BRONZE}."{source_id}__{table}"' if schema == "public"
           else f'{BRONZE}."{source_id}__{schema}__{table}"')
    src = f'{CATALOG.get(source_id, source_id)}."{schema}"."{table}"'
    rows = _trino(
        f'SELECT count(*) FROM (SELECT "{pk}" FROM {tgt} '
        f'EXCEPT SELECT "{pk}" FROM {src}) g')
    ghosts = int(rows[0]) if rows else 0
    return {"asset": f"{source_id}.{table}", "ghost_rows": ghosts,
            "note": "源系统已删除但 bronze 仍存在的行" if ghosts else "无幽灵数据"}
