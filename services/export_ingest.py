"""SaaS / 文件导出的数据面（readme 8.2）。

**导出优先，不写 API 适配器。** Salesforce / HubSpot / 飞书都支持定时报表自动发邮件，
把收件人设成 Claw 的地址，这条链路就落在 16.4 已有的邮件附件路径上：
发件人白名单、内容嗅探、大小上限、`Message-ID` 溯源，一整套约束现成。

这里补的是唯一缺的那一环：**payload → bronze**。

    附件 / 导出文件 ──► stage.saas_export.<t>（暂存）──sync_table──► iceberg.bronze
                            ↑ 独立写账号                    ↑ 复用数据库那条路

复用而不是新写，换来三件现成的东西：类型映射、增量水位、schema 漂移检测。
Agent 对 stage 只有 SELECT —— 它跟源系统一样是只读的。

导出路径的两个意外优点（8.2）：

    分页不是快照   一次导出就是一个快照，不存在翻页中记录跳页
    删除看不见     两次快照做主键集合差即可 —— `deleted_keys()`

要认的两个代价：

    类型全变文本   bronze 本来就原样落地，blank_rate 已在处理
    列名是标签     导出给「客户来源」，API 给 `LeadSource`。业务方改显示名列名就变，
                   因此**映射必须让人确认一次并记进记忆层**（`confirm_column_mapping`）
"""
import hashlib
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STAGE_SCHEMA = "saas_export"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExportError(RuntimeError):
    pass


def _ident(name, what="标识符"):
    if not IDENT.match(str(name or "")):
        raise ExportError(f"{what}不合法：{name!r}")
    return name


def _admin_dsn():
    """暂存区的**写**账号。Agent 拿不到它 —— 它只有 stage 的 SELECT。"""
    import connector
    from urllib.parse import urlsplit, urlunsplit
    raw = (connector._E.get("STAGE_ADMIN_DSN")
           or connector._E.get("SOURCE_DSN", ""))
    if not raw:
        raise ExportError("没有配置 STAGE_ADMIN_DSN / SOURCE_DSN")
    u = urlsplit(raw)
    if not connector._E.get("STAGE_ADMIN_DSN"):
        host = u.netloc.split("@")[-1]
        u = u._replace(netloc=f"{connector._E.get('STAGE_ADMIN_USER', 'postgres')}:"
                              f"{connector._E.get('STAGE_ADMIN_PASSWORD', 'postgres')}@{host}")
    return urlunsplit(u._replace(path="/stage", query=""))


def _stage_table(saas_source, table):
    return f"{_ident(saas_source, '源名')}__{_ident(table, '表名')}"


# ---------------------------------------------------------------- 列名映射
def normalize_columns(columns) -> list:
    """把导出的中文/带空格标签变成可用作列名的标识符。

    **只做机械归一，不猜业务含义** —— 「客户来源」到底对应 `LeadSource` 还是
    `Source__c`，机器猜不了，必须人确认（见 `confirm_column_mapping`）。
    """
    out, seen = [], {}
    for i, c in enumerate(columns):
        base = re.sub(r"[^0-9A-Za-z_]+", "_", str(c or "").strip()).strip("_").lower()
        if not base or base[0].isdigit():
            base = f"c{i}_{base}" if base else f"c{i}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base}_{n}")
    return out


def column_mapping(columns) -> dict:
    """导出列 → 归一列名。给人确认用的那张表。"""
    return dict(zip([str(c) for c in columns], normalize_columns(columns)))


def confirm_column_mapping(asset: str, mapping: dict, confirmed_by: str):
    """把确认过的映射记进记忆层 —— 下次不再问（readme 5.7）。"""
    from plugins.datasteward_gate.approvals import open_store
    with open_store(readonly=False, init_schema=True) as st:
        st.remember(asset, "export_column_mapping",
                    json.dumps(mapping, ensure_ascii=False), confirmed_by)
    return {"asset": asset, "confirmed_by": confirmed_by, "columns": len(mapping)}


def known_column_mapping(asset: str) -> dict | None:
    from plugins.datasteward_gate.approvals import open_store
    try:
        with open_store(readonly=True, init_schema=False) as st:
            v = st.known(asset, "export_column_mapping")
    except Exception:                                        # noqa: BLE001
        return None
    if not v:
        return None
    try:
        return json.loads(v["value"] if isinstance(v, dict) else v[0])
    except Exception:                                        # noqa: BLE001
        return None


# ---------------------------------------------------------------- 暂存
def stage_payload(saas_source: str, table: str, payload: dict) -> dict:
    """把一份导出 payload 写进暂存区。

    全部列建成 text —— 导出本来就是文本，**在这里猜类型只会猜错**；
    类型推断留给 bronze→silver，那时有人确认过口径。
    """
    import psycopg

    cols = normalize_columns(payload["columns"])
    if not cols:
        raise ExportError("导出文件没有列")
    st = _stage_table(saas_source, table)
    ddl = ", ".join(f'"{c}" text' for c in cols)
    rows = payload["rows"]

    with psycopg.connect(_admin_dsn(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{STAGE_SCHEMA}"')
            cur.execute(f'DROP TABLE IF EXISTS "{STAGE_SCHEMA}"."{st}"')
            cur.execute(f'CREATE TABLE "{STAGE_SCHEMA}"."{st}" ({ddl})')
            if rows:
                with cur.copy(f'COPY "{STAGE_SCHEMA}"."{st}" '
                              f'({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                              f'FROM STDIN') as cp:
                    for r in rows:
                        vals = [("" if v is None else str(v)) for v in r][:len(cols)]
                        vals += [""] * (len(cols) - len(vals))
                        cp.write("\t".join(
                            v.replace("\\", "\\\\").replace("\t", " ")
                             .replace("\n", " ").replace("\r", " ")
                            for v in vals) + "\n")
            cur.execute(f'GRANT SELECT ON "{STAGE_SCHEMA}"."{st}" TO agent_ro')
        conn.commit()
    return {"stage_table": f"{STAGE_SCHEMA}.{st}", "rows": len(rows),
            "columns": cols}


# ---------------------------------------------------------------- 快照差分
def _keys(saas_source, table, pk):
    import psycopg
    st, pk = _stage_table(saas_source, table), _ident(pk, "主键列")
    with psycopg.connect(_admin_dsn(), connect_timeout=10) as conn, conn.cursor() as cur:
        try:
            cur.execute(f'SELECT "{pk}" FROM "{STAGE_SCHEMA}"."{st}"')
        except Exception:                                    # noqa: BLE001
            return None
        return {r[0] for r in cur.fetchall()}


def deleted_keys(saas_source: str, table: str, pk: str, new_payload: dict) -> dict:
    """两次快照做主键集合差 —— 导出路径**能看见删除**，API 增量看不见。

    在覆盖暂存表之前调用：拿当前暂存的主键集合与新导出的比。
    """
    pk_norm = normalize_columns([pk])[0]
    old = _keys(saas_source, table, pk_norm)
    cols = normalize_columns(new_payload["columns"])
    if pk_norm not in cols:
        raise ExportError(f"新导出里没有主键列 {pk}（归一后 {pk_norm}）")
    i = cols.index(pk_norm)
    new = {str(r[i]) for r in new_payload["rows"] if i < len(r)}
    if old is None:
        return {"first_snapshot": True, "deleted": [], "added": sorted(new)[:50],
                "note": "首次快照，无可比对象"}
    old = {str(x) for x in old}
    return {"first_snapshot": False,
            "deleted": sorted(old - new), "added": sorted(new - old),
            "deleted_count": len(old - new), "added_count": len(new - old),
            "note": "导出快照差分：API 增量在 updated_at 水位线上看不到删除"}


# ---------------------------------------------------------------- 一条龙
def ingest_export(saas_source: str, table: str, path: str | None = None,
                  payload: dict | None = None, pk: str | None = None) -> dict:
    """导出文件 → 暂存 → bronze。返回含删除对账结果。

    `path` 与 `payload` 二选一；`path` 走 16.4 的只读解析（不执行宏）。
    """
    import ingest_file
    import sync

    if payload is None:
        if not path:
            raise ExportError("需要 path 或 payload")
        payload = ingest_file.read_any(path)

    asset = f"{saas_source}.{table}"
    diff = deleted_keys(saas_source, table, pk, payload) if pk else None
    staged = stage_payload(saas_source, table, payload)
    r = sync.sync_table("stage", _stage_table(saas_source, table),
                        schema=STAGE_SCHEMA, allow_schema_change=True,
                        bronze_name=f"{saas_source}__{table}")
    return {"asset": asset, "source_kind": "export", **staged,
            "bronze_table": r["bronze_table"], "bronze_rows": r["row_count"],
            "deletions": diff,
            "fingerprint": ingest_file.canonical_fingerprint(payload),
            "column_mapping_confirmed": known_column_mapping(asset) is not None}
