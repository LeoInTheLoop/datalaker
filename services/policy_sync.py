"""Policy Sync（readme 11.3）—— **策略绑定在 tag 上，不绑定在表名上。**

    分类（PII / Confidential / Internal / Public）+ Owner
            │
       Policy Sync（生成策略）
            │
       ┌────┴────┐
    Trino 授权   MinIO 前缀授权
    (列遮蔽)      (对象级)

带来的效果：新表接入时只要打好 tag 就自动继承策略，不需要逐表人工配权限。
**这是权限治理能规模化的唯一方式** —— 几百张表逐表配一遍不可能维护。

## 为什么先生成文件而不是上 OPA

readme 画的是 Trino OPA plugin + Open Policy Agent。但 R3 已经落地的是
Trino 的 file-based access control，它同样支持按 principal 的列遮蔽，
**而 file 与 OPA 的输入是同一份事实（分类 + owner）**。
先把「事实 → 策略」这一步做对，策略引擎换成 OPA 时改的是渲染函数，
不是数据来源。上来就引入 OPA 会多一个必须一直活着的进程，
换不来任何这一阶段需要的能力。TODO(R5)：需要行级过滤时再换。

## 分类存哪

R1–R4 用轻量 `asset_semantics` 表代替 OpenMetadata（见 CLAUDE.md 环境约束）。
接口在这里收口，将来换成 catalog 的 REST 只改 `_classifications()`。
"""
import json
import os
import pathlib

LEVELS = ("PII", "Confidential", "Internal", "Public")
CLASS_KEY = "classification"
COL_PREFIX = "column_class:"

# 分类 → 谁能看到什么。**这张表就是策略本身**，改策略不用改代码。
MATRIX = {
    "PII":          {"claw": "raw", "analyst": "mask", "owner": "raw"},
    "Confidential": {"claw": "raw", "analyst": "mask", "owner": "raw"},
    "Internal":     {"claw": "raw", "analyst": "raw",  "owner": "raw"},
    "Public":       {"claw": "raw", "analyst": "raw",  "owner": "raw"},
}
MASK_EXPR = "'***'"


def _store(readonly=True):
    from plugins.datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=not readonly)


# ---------------------------------------------------------------- 打标
def classify(asset: str, level: str, by: str) -> dict:
    """给一张表打分类。**确认过才算数** —— 分类是业务判断，不是技术判断。"""
    if level not in LEVELS:
        raise ValueError(f"未知分类 {level}，可选 {LEVELS}")
    with _store(readonly=False) as st:
        st.remember(asset, CLASS_KEY, level, by)
    return {"asset": asset, "classification": level, "confirmed_by": by}


def classify_column(asset: str, column: str, level: str, by: str) -> dict:
    if level not in LEVELS:
        raise ValueError(f"未知分类 {level}")
    with _store(readonly=False) as st:
        st.remember(asset, COL_PREFIX + column, level, by)
    return {"asset": asset, "column": column, "classification": level}


def _known(st, asset, key):
    v = st.known(asset, key)
    if not v:
        return None
    return v["value"] if isinstance(v, dict) else v[0]


def classifications(assets: list) -> dict:
    """读回分类。换成 OpenMetadata 时只改这一个函数。"""
    out = {}
    with _store() as st:
        for a in assets:
            lvl = _known(st, a, CLASS_KEY)
            cols = {}
            # 列级分类没有前缀查询接口，按已知列名逐个取
            for c in _column_candidates(st, a):
                cl = _known(st, a, COL_PREFIX + c)
                if cl:
                    cols[c] = cl
            if lvl or cols:
                out[a] = {"level": lvl, "columns": cols}
    return out


def _column_candidates(st, asset):
    """哪些列被打过标。asset_semantics 没有前缀扫描，这里直接查底表。"""
    try:
        if hasattr(st.db, "execute") and not hasattr(st.db, "cursor_factory"):
            rows = st.db.execute(
                "SELECT key FROM asset_semantics WHERE asset=? AND key LIKE ?",
                (asset, COL_PREFIX + "%")).fetchall()
        else:
            with st.db.cursor() as c:
                c.execute("SELECT key FROM asset_semantics WHERE asset=%s"
                          " AND key LIKE %s", (asset, COL_PREFIX + "%"))
                rows = c.fetchall()
    except Exception:                                        # noqa: BLE001
        return []
    return [r[0][len(COL_PREFIX):] for r in rows]


# ---------------------------------------------------------------- 生成
def generate(assets: list, catalog: str = "iceberg",
             base: dict | None = None) -> dict:
    """由分类生成 Trino 授权规则。

    输出是完整的 rules.json —— `base` 里的非策略部分（catalog 授权、
    admin 规则）原样保留，**只有由 tag 推导出来的表规则被重写**。
    这样手工配的基础授权不会被 Policy Sync 冲掉。
    """
    rules = json.loads(json.dumps(base or {"catalogs": [], "schemas": [],
                                           "tables": []}))
    cls = classifications(assets)

    generated = []
    for asset, info in sorted(cls.items()):
        schema, _, table = asset.rpartition(".")
        schema = schema or "bronze"
        masked = [c for c, lv in info["columns"].items()
                  if MATRIX.get(lv, {}).get("analyst") == "mask"]
        if info["level"] in ("PII", "Confidential") and not masked:
            # 整表敏感但没标列：analyst 直接看不到这张表
            generated.append({"user": "analyst", "catalog": catalog,
                              "schema": schema, "table": table,
                              "privileges": [],
                              "_reason": f"整表 {info['level']}，未标列 → 不可见"})
            continue
        if masked:
            generated.append({
                "user": "analyst", "catalog": catalog, "schema": schema,
                "table": table, "privileges": ["SELECT"],
                "columns": [{"name": c, "mask": MASK_EXPR} for c in sorted(masked)],
                "_reason": f"列分类 {sorted(masked)} → 遮蔽"})

    # 保留非本工具生成的规则（没有 _reason 标记的）
    kept = [r for r in rules.get("tables", []) if "_reason" not in r]
    rules["tables"] = kept + generated
    return rules


def strip_meta(rules: dict) -> dict:
    """去掉 `_` 开头的说明字段。

    **Trino 的 file access control 严格校验，未知字段会让它起不来**
    （R3 handoff 踩过一次：`rules.json` 里放 `_comment` 直接启动失败）。
    理由不能丢，所以落到旁路的 `.why.json` 里 —— 策略要可复核。
    """
    out = json.loads(json.dumps(rules))
    for sec in ("catalogs", "schemas", "tables"):
        out[sec] = [{k: v for k, v in r.items() if not k.startswith("_")}
                    for r in out.get(sec, [])]
    return out


def write(rules: dict, path: str | None = None) -> str:
    p = pathlib.Path(path or os.environ.get(
        "TRINO_RULES_PATH", "infra/trino/etc/rules.json"))
    why = [{"table": r.get("table"), "user": r.get("user"),
            "reason": r["_reason"]}
           for r in rules.get("tables", []) if "_reason" in r]
    p.write_text(json.dumps(strip_meta(rules), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    p.with_suffix(".why.json").write_text(
        json.dumps(why, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(p)


def load(path: str | None = None) -> dict:
    p = pathlib.Path(path or os.environ.get(
        "TRINO_RULES_PATH", "infra/trino/etc/rules.json"))
    return json.loads(p.read_text(encoding="utf-8"))


def diff(before: dict, after: dict) -> dict:
    """改了什么。**策略变更必须可复核** —— 直接写进去等于没人知道发生了什么。"""
    def key(r):
        return (r.get("user"), r.get("catalog"), r.get("schema"), r.get("table"))
    b = {key(r): r for r in before.get("tables", [])}
    a = {key(r): r for r in after.get("tables", [])}
    return {
        "added": [a[k] for k in a.keys() - b.keys()],
        "removed": [b[k] for k in b.keys() - a.keys()],
        "changed": [{"before": b[k], "after": a[k]}
                    for k in a.keys() & b.keys() if a[k] != b[k]],
    }


def sync(assets: list, path: str | None = None, dry_run: bool = True) -> dict:
    """一步到位：读分类 → 生成 → 比对 →（可选）落盘。

    **默认 dry_run** —— 策略是会让人干不了活的东西，改它要人点头。
    """
    cur = load(path)
    new = generate(assets, base=cur)
    d = diff(cur, new)
    written = None
    if not dry_run:
        written = write(new, path)
    return {"assets": len(assets), "diff": d, "written": written,
            "dry_run": dry_run,
            "note": "Trino 需重载配置才生效（重启容器或热加载）"}
