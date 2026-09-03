"""Dirty Data Injector —— 把干净数据集变成「源系统」，同时产出 ground truth。

readme 16.1：
    干净数据集 ──inject──► 脏数据集（作为源系统，只读）
                              │ Agent 抽取
                              ▼
                          bronze ──清洗──► silver ◄──对比──► 干净数据集

**注入器必须绕过 Connector。** Agent 的账号只有 SELECT（铁律 3），
注入是「布置考场」不是「参加考试」，因此用自己的 DSN 直连。
这也是它不 import services/ 的原因——两条通路必须物理分开。

**干净副本留在 public，脏副本落在独立 schema**，所以：
  · 可重复：删掉 schema 重跑即可，原数据永不受损
  · 可取证：snapshot.py 事后比对脏 schema 前后是否被 Agent 改过（铁律 4）

跑法：
    python3 evals/injector.py --case evals/cases/olist_dq_v1.json --out evals/report/run1
    python3 evals/injector.py --case ... --dry-run      # 不连库，只打印将执行的 SQL
"""
import argparse
import json
import pathlib
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLE_CAP = 200          # manifest 里每条注入最多留这么多行的原值，够对比又不撑爆文件


def _env(path=None):
    """读 .env 当**数据文件**读。不 import services.connector —— 那会破坏体外隔离。"""
    d, p = {}, pathlib.Path(path or ROOT / ".env")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


def dsn_for(case, env=None):
    """注入器的 DSN。

    **必须与 Agent 用不同账号**：`SOURCE_DSN` 里是 `agent_ro`，只有 SELECT（铁律 3）。
    布置考场要写权限，因此默认换成管理账号并切到本 case 的库。
    显式设 `EVAL_SOURCE_DSN` 时原样使用（只换库名）。
    """
    e = env or _env()
    db = case.get("db") or case.get("source_id")
    raw = e.get("EVAL_SOURCE_DSN") or e.get("SOURCE_DSN", "")
    if not raw:
        return ""
    u = urlsplit(raw)
    if not e.get("EVAL_SOURCE_DSN"):
        host = u.netloc.split("@")[-1]
        u = u._replace(netloc=f"{e.get('EVAL_PG_USER', 'postgres')}:"
                              f"{e.get('EVAL_PG_PASSWORD', 'postgres')}@{host}")
    return urlunsplit(u._replace(path=f"/{db}"))


def _ident(name):
    if not name.replace("_", "").isalnum():
        raise ValueError(f"非法标识符：{name}")
    return name


# --------------------------------------------------------------- 各类注入
# 每个函数返回 (sql_list, 说明)。行的选取一律 ORDER BY <pk> LIMIT n ——
# **确定性优于随机**：跨 PG 版本、跨机器都选中同一批行，seed 才真的可复现。
def _pick(schema, table, pk, n, offset=0):
    return (f'SELECT "{pk}" FROM "{schema}"."{table}" '
            f'ORDER BY "{pk}" LIMIT {int(n)} OFFSET {int(offset)}')


def build(inj, schema, pk):
    t, c = _ident(inj["table"]), _ident(inj.get("column") or pk)
    n, kind = int(inj.get("n", 10)), inj["kind"]
    q = f'"{schema}"."{t}"'
    sel = _pick(schema, t, pk, n, inj.get("offset", 0))

    if kind in ("null_burst", "ambiguous_semantics"):
        return [f'UPDATE {q} SET "{c}" = NULL WHERE "{pk}" IN ({sel})'], "置 NULL"
    if kind == "duplicate_rows":
        return [f'INSERT INTO {q} SELECT * FROM {q} WHERE "{pk}" IN ({sel})'], "整行复制"
    if kind == "broken_fk":
        # **按列类型造假键。** 文本列首字符换 `~`（等长，不撑爆 varchar）；
        # 数值列取负（负 ID 不可能存在，且不会溢出原类型）。
        # 早先一律拼 `'ghost_' || col`，在 varchar(5) 上撑爆、在 smallint 上类型不符。
        dt = (inj.get("_dtype") or "text").lower()
        if any(x in dt for x in ("int", "numeric", "real", "double", "decimal")):
            expr = f'-abs("{c}")'
            guard = f' AND "{c}" IS NOT NULL AND "{c}" <> 0'
        else:
            expr = f'\'~\' || substr("{c}"::text, 2)'
            guard = f' AND "{c}" IS NOT NULL AND length("{c}"::text) > 1'
        return [f'UPDATE {q} SET "{c}" = {expr} WHERE "{pk}" IN ({sel}){guard}'], \
            "指向不存在的父行"
    if kind in ("enum_drift", "spelling_drift"):
        # **从现有值派生变体，不用固定字面量。**
        # 写死 'São Paulo' 塞不进 varchar(5)，而为注入去改列宽等于顺手
        # 制造一次 schema 漂移。派生变体等字符长度，也更像真实漂移 ——
        # 真实的大小写/音标漂移本来就是同一个值的不同写法。
        if kind == "enum_drift":
            forms = [f'"{c}"::text', f'upper("{c}"::text)',
                     f'initcap("{c}"::text)']
            how = "大小写漂移（由原值派生）"
        else:
            forms = [f'"{c}"::text', f'upper("{c}"::text)',
                     f"translate(\"{c}\"::text, 'aeo', 'áéó')"]
            how = "音标漂移（由原值派生，等长）"
        cases = " ".join(f"WHEN {i} THEN {f}" for i, f in enumerate(forms))
        sql = (f'UPDATE {q} SET "{c}" = CASE'
               f' (abs(hashtext("{pk}"::text)) % {len(forms)}) {cases} END'
               f' WHERE "{pk}" IN ({sel}) AND "{c}" IS NOT NULL')
        return [sql], how
    if kind == "date_anomaly":
        # CSV 导入的表里日期常是 text —— 一律显式转型，别指望列类型
        base = _ident(inj.get("base_column", "order_purchase_timestamp"))
        # **不要再转回 text**：olist 的日期列是 text，northwind 的是 date。
        # 转成 text 再赋值给 date 列会直接类型不匹配；反过来 PG 的赋值转换
        # 能把 timestamp 写进 text 列。少一层转换，两边都成立。
        return [f"""UPDATE {q} SET "{c}" = ("{base}"::timestamp - interval '3 days')
                    WHERE "{pk}" IN ({sel}) AND "{base}" IS NOT NULL
                      AND btrim("{base}"::text) <> ''"""], "送达早于下单"
    if kind == "unit_error":
        f = inj.get("factor", 100)
        return [f"""UPDATE {q} SET "{c}" = ("{c}"::numeric * {int(f)})
                    WHERE "{pk}" IN ({sel}) AND "{c}" IS NOT NULL
                      AND btrim("{c}"::text) <> ''"""], f"数量级 ×{f}"
    if kind == "type_error":
        v = str(inj.get("bad_value", "unknown")).replace("'", "''")
        # 先放宽列类型 —— bronze 原样落地意味着源里真的可能是文本
        return [f'ALTER TABLE {q} ALTER COLUMN "{c}" TYPE text',
                f"""UPDATE {q} SET "{c}" = '{v}' WHERE "{pk}" IN ({sel})"""], "类型污染"
    if kind == "cross_table_conflict":
        tot = inj.get("total_column")
        if not tot:
            return [], None      # 无总额列可改 —— 见下方 skipped 处理
        return [f'UPDATE {q} SET "{_ident(tot)}" = "{_ident(tot)}" * 2 '
                f'WHERE "{pk}" IN ({sel})'], "总额与明细不符"
    raise ValueError(f"未知注入类型：{kind}")


# --------------------------------------------------------------- 主流程
def inject(case, dsn, schema, dry_run=False):
    conn = None
    if not dry_run:
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=10)

    tables = sorted({i["table"] for i in case["injections"]})
    manifest = {"case_id": case["case_id"], "schema": schema, "seed": case.get("seed"),
                "source_id": case.get("source_id"), "tables": tables,
                "injections": [], "skipped": [], "sql_log": []}

    def run(sql, fetch=False):
        manifest["sql_log"].append(sql)
        if dry_run:
            return []
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if fetch else []

    # 1. 建脏副本（原数据永不受损）
    run(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    dup_tables = {i["table"] for i in case["injections"]
                  if i["kind"] == "duplicate_rows"}
    manifest["pk_preserved"] = {}
    for t in tables:
        t = _ident(t)
        run(f'DROP TABLE IF EXISTS "{schema}"."{t}" CASCADE')
        run(f'CREATE TABLE "{schema}"."{t}" AS TABLE public."{t}"')
        # CREATE TABLE AS **不复制约束**。不补回来的话主键唯一性检查根本无从触发，
        # 测出来的低召回是注入器的伪影，不是被测系统的缺陷。
        pk = _pk_of(conn, t, dry_run)
        keep = pk not in ("ctid", None) and t not in dup_tables
        manifest["pk_preserved"][t] = keep
        if keep:
            run(f'ALTER TABLE "{schema}"."{t}" ADD PRIMARY KEY ("{pk}")')

    # 记下源表的真实主外键。
    # `CREATE TABLE AS` 不复制约束，脏副本上查不到它们；而外键完整性与
    # 全量主键唯一只能在 lake 上算（源库禁 join，铁律 3），那时需要知道键是什么。
    # 这属于「考场的结构信息」，不是答案 —— 真实 Agent 同样读得到源库的约束。
    manifest["keys"] = _keys_of(conn, tables, dry_run)

    # Agent 在考场里同样只有 SELECT —— 铁律 3 在 eval 内部照样成立
    role = _ident(_env().get("EVAL_AGENT_ROLE", "agent_ro"))
    run(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}')
    run(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO {role}')

    # 2. 逐条注入，先留原值当 ground truth
    for inj in case["injections"]:
        t, c = _ident(inj["table"]), inj.get("column")
        pk = inj.get("pk") or _pk_of(conn, t, dry_run)
        dtype = _dtype_of(conn, schema, t, c, dry_run)
        try:
            stmts, how = build({**inj, "column": c, "_dtype": dtype}, schema, pk)
        except ValueError as e:
            manifest["skipped"].append({"id": inj["id"], "reason": str(e)})
            continue
        if not stmts:
            manifest["skipped"].append({
                "id": inj["id"],
                "reason": f"{inj['kind']} 需要 total_column，本数据集没有总额列 —— "
                          f"不伪造，如实记为未注入"})
            continue

        before = run(f'SELECT "{pk}", "{c}" FROM "{schema}"."{t}" '
                     f'WHERE "{pk}" IN ({_pick(schema, t, pk, inj.get("n", 10), inj.get("offset", 0))}) '
                     f'LIMIT {SAMPLE_CAP}', fetch=True) if c else []
        # **注入前后对一次账。**
        # 派生变体在特定数据上可能是空操作 —— 值本来就是大写时 `upper(col)`
        # 等于没改。这种「注了但没变」会被判成漏检，把规则的分扣在
        # 注入器头上。测不准的东西不该拿来评分，所以宁可如实记成未注入。
        before_ck = (run(f'SELECT md5(coalesce(string_agg("{c}"::text, \'\' '
                         f'ORDER BY "{pk}"::text), \'\')) '
                         f'FROM "{schema}"."{t}"', fetch=True) if c else None)
        affected = 0
        for s in stmts:
            run(s)
        after_ck = (run(f'SELECT md5(coalesce(string_agg("{c}"::text, \'\' '
                        f'ORDER BY "{pk}"::text), \'\')) '
                        f'FROM "{schema}"."{t}"', fetch=True) if c else None)
        if (not dry_run and before_ck and after_ck
                and before_ck[0][0] == after_ck[0][0]):
            manifest["skipped"].append({
                "id": inj["id"],
                "reason": f"{inj['kind']} 在 {t}.{c} 上是空操作（值未发生变化）——"
                          f"如实记为未注入，不计入召回分母"})
            continue
        if not dry_run:
            affected = conn.cursor().execute(
                f'SELECT count(*) FROM "{schema}"."{t}"').fetchone()[0]
        manifest["injections"].append({
            **inj,
            "pk": pk, "how": how,
            "clean_values": {str(r[0]): (None if r[1] is None else str(r[1]))
                             for r in before},
            "table_rows_after": affected,
        })

    if conn:
        conn.commit()
        conn.close()
    return manifest


def _dtype_of(conn, schema, table, column, dry_run):
    """列的数据类型。注入方式依赖它 —— 同一种缺陷在 text 列和数值列上
    要用完全不同的写法，猜错就是一次类型不匹配。"""
    if dry_run or conn is None or not column:
        return "text"
    with conn.cursor() as cur:
        cur.execute("SELECT data_type FROM information_schema.columns"
                    " WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                    (schema, table, column))
        r = cur.fetchone()
    return r[0] if r else "text"


def _keys_of(conn, tables, dry_run):
    """从 public schema 读主键与外键。dry-run 时返回空。"""
    if dry_run or conn is None:
        return {}
    out = {}
    with conn.cursor() as cur:
        for t in tables:
            cur.execute("""SELECT a.attname FROM pg_index i
                           JOIN pg_attribute a ON a.attrelid=i.indrelid
                            AND a.attnum = ANY(i.indkey)
                           WHERE i.indrelid = %s::regclass AND i.indisprimary
                           ORDER BY a.attnum""", (f"public.{t}",))
            pk = [r[0] for r in cur.fetchall()]
            cur.execute("""SELECT kcu.column_name, ccu.table_name, ccu.column_name
                           FROM information_schema.table_constraints tc
                           JOIN information_schema.key_column_usage kcu
                             ON kcu.constraint_name = tc.constraint_name
                           JOIN information_schema.constraint_column_usage ccu
                             ON ccu.constraint_name = tc.constraint_name
                           WHERE tc.constraint_type='FOREIGN KEY'
                             AND tc.table_schema='public' AND tc.table_name=%s""",
                        (t,))
            fks = [{"column": a, "ref_table": b, "ref_column": c}
                   for a, b, c in cur.fetchall()]
            out[t] = {"pk": pk, "fks": fks}
    return out


def _pk_of(conn, table, dry_run):
    """主键列名。拿不到就退回 ctid —— 注入需要稳定排序键，不需要语义正确。"""
    if dry_run or conn is None:
        return {"orders": "order_id", "order_items": "order_id",
                "customers": "customer_id", "sellers": "seller_id",
                "products": "product_id"}.get(table, "id")
    with conn.cursor() as cur:
        cur.execute("""SELECT a.attname FROM pg_index i
                       JOIN pg_attribute a ON a.attrelid=i.indrelid
                        AND a.attnum = ANY(i.indkey)
                       WHERE i.indrelid = %s::regclass AND i.indisprimary
                       ORDER BY a.attnum LIMIT 1""", (f"public.{table}",))
        r = cur.fetchone()
    return r[0] if r else "ctid"


def main(argv=None):
    ap = argparse.ArgumentParser(description="注入已知缺陷并产出 ground truth")
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", default="evals/report/last")
    ap.add_argument("--schema", default=None, help="脏副本 schema，默认 eval_<case>")
    ap.add_argument("--dsn", default=None, help="默认取 .env 的 EVAL_SOURCE_DSN / SOURCE_DSN")
    ap.add_argument("--dry-run", action="store_true", help="不连库，只产出将执行的 SQL")
    a = ap.parse_args(argv)

    case = json.loads(pathlib.Path(a.case).read_text(encoding="utf-8"))
    dsn = a.dsn or dsn_for(case)
    if not dsn and not a.dry_run:
        print("没有 DSN：设 EVAL_SOURCE_DSN 或用 --dry-run", file=sys.stderr)
        return 2
    schema = a.schema or ("eval_" + case["case_id"].replace("-", "_"))

    m = inject(case, dsn, schema, a.dry_run)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"注入 {len(m['injections'])} 条，跳过 {len(m['skipped'])} 条 "
          f"-> {out / 'manifest.json'}"
          + ("   [dry-run，未连库]" if a.dry_run else ""))
    for s in m["skipped"]:
        print(f"  跳过 {s['id']}：{s['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
