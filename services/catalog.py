"""资产档案 —— 认知可复用（R6 闭环 A）。

**一次采集，之后不再回源库。** 在这之前 `describe_asset` 每调一次就是
三趟源库往返（列、外键、lake 清单），新会话等于从零开始；而「这张表长
什么样」是**上一次已经问过**的事。

三条规矩，都是踩过的坑换来的：

1. **观测事实只由这里写。** `record_observed()` 是 `asset_catalog` 里
   `status='observed'` 的唯一入口，而它只接受 Connector 采回来的东西。
   没有任何工具让模型写这一层 —— 模型能改「源库里到底有什么」的那一刻，
   整套档案就不可信了。推断和确认走另外的入口，各自成行、互不覆盖。

2. **读档要说清楚是什么时候的。** 每次输出都带快照时刻。省掉往返的代价
   是答案有延迟，把延迟藏起来就变成了撒谎。

3. **旧档案是比较基线，不能证明源库没变。** 想知道变没变，必须有**新的
   观测**（`observe()`）—— 那是闭环 B 的巡检要做的事。以为存了档案就
   「知道」源库现在什么样，是这条线上最容易犯的错。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCHEMA_KINDS = ("schema", "foreign_keys")


def _store(readonly=False):
    from plugins.datasteward_gate.approvals import open_store
    # init_schema=True：DDL 幂等，而存量库建于 asset_catalog 之前 ——
    # 不建表的话读档报「no such table」，看起来像「这张表无档」，
    # 实际是库没升级。两者差很远（`trace_asset` 那处注释同理）。
    return open_store(readonly=readonly, init_schema=True)


def asset_name(source: str, table: str) -> str:
    return f"{source}.{table}"


# ------------------------------------------------------------------ 写：观测
def record_observed(source: str, table: str, meta: dict,
                    fks=None, actor: str = "") -> dict:
    """把 Connector 采回来的结构落档。**只写 observed 层。**

    `meta` 就是 `connector.describe_table()` 的返回值；`fks` 是
    `connector.list_foreign_keys()` 里与这张表相关的那些。
    内容没变则不写新行（幂等），返回各条目实际写没写。
    """
    asset = asset_name(source, table)
    # 列名、类型、可空性、主键、外键 —— 五项都进档案。
    # `sync._schema_hash` 只覆盖前两项，改它会让存量资产集体报 SchemaDrift，
    # 那笔账在闭环 B 一起还。
    cols = [[str(c[0]), str(c[1]), str(c[2])] for c in (meta.get("columns") or [])]
    # **一列都没采到 ≠ 这张表有 0 列。**
    # `connector.describe_table` 对不存在的表返回 `{"columns": []}` 而不是
    # 报错（information_schema 查不到就是空结果集）。照单全收的后果是：
    # 一张已建档的表因为权限变化或连接抖动读不到时，档案里的真实结构
    # 会被**覆盖成空** —— 认知被自己的采集破坏掉，而且看起来一切正常。
    # 空结构一律不落档，由调用方去判断是「表没了」还是「这次没读到」。
    if not cols:
        return {"asset": asset, "wrote": {}, "empty": True,
                "why": "没有采到任何列 —— 表不存在、无权限、或这次读取失败"}
    pk = [str(x) for x in (meta.get("primary_key") or [])]
    rel = [[str(x) for x in f] for f in (fks or [])]
    who = actor or f"connector:{source}"
    ev = {"via": "information_schema + pg_catalog", "source": source}

    st = _store()
    wrote = {}
    wrote["columns"] = st.catalog_put(
        asset, "schema", "columns", cols, "observed", who, evidence=ev)
    wrote["primary_key"] = st.catalog_put(
        asset, "schema", "primary_key", pk, "observed", who, evidence=ev)
    if fks is not None:
        wrote["foreign_keys"] = st.catalog_put(
            asset, "foreign_keys", "declared", rel, "observed", who, evidence=ev)
    return {"asset": asset, "wrote": {k: v for k, v in wrote.items() if v}}


def record_foreign_keys(source: str, fks, actor: str = "") -> dict:
    """整个源的外键清单，按表拆开落档。

    外键是**源级**的一次查询（`pg_constraint` 全扫），但用的时候是按表问的。
    在这里拆开，读档那侧就不必每次全量拉一遍再过滤。
    """
    who = actor or f"connector:{source}"
    ev = {"via": "pg_constraint", "source": source}
    by_table = {}
    for f in fks or []:
        t, c, rt, rc = [str(x) for x in f]
        by_table.setdefault(t.split(".")[-1], []).append([t, c, rt, rc])
        by_table.setdefault(rt.split(".")[-1], []).append([t, c, rt, rc])
    st = _store()
    wrote = {}
    for tbl, rel in by_table.items():
        wid = st.catalog_put(asset_name(source, tbl), "foreign_keys", "declared",
                             sorted(rel), "observed", who, evidence=ev)
        if wid:
            wrote[tbl] = wid
    return {"tables": len(by_table), "wrote": wrote}


def observe(source: str, table: str) -> dict:
    """采集 + 落档。**这是唯一会碰源库的函数**，其余都只读治理库。

    落档动作在 `connector` 里（采集即建档），这里只负责把两次采集都触发
    到，再把刚落下的档读回来。外键读不到不让整次建档失败 —— 结构比关系
    重要；但也**不记成「没有外键」**，档案里没这一条，与「确认过没有」
    区分得开。
    """
    import connector
    connector.describe_table(source, table)
    try:
        fks = connector.list_foreign_keys(source)
    except Exception:                                        # noqa: BLE001
        return lookup(source, table)
    # 全源扫过一遍都没有涉及这张表的外键 —— 那是**观测到的「没有」**，
    # 得显式记一条空的。不记的话档案里没这一条，读档那侧只能说「没采到」，
    # 而「源库声明了没有关系」和「我们没看过」是两个完全不同的结论。
    if not any(str(f[0]).split(".")[-1] == table
               or str(f[2]).split(".")[-1] == table for f in fks):
        _store().catalog_put(asset_name(source, table), "foreign_keys",
                             "declared", [], "observed", f"connector:{source}",
                             evidence={"via": "pg_constraint", "source": source})
    return lookup(source, table)


# ------------------------------------------------------------------ 读：档案
def lookup(source: str, table: str) -> dict | None:
    """只读治理库，**不碰源库**。无档返回 None。

    返回里 `observed_at` 是这份快照的时刻，`stale_hours` 是它的年龄 ——
    调用方必须把它显示出来，见模块开头第 2 条。
    """
    asset = asset_name(source, table)
    st = _store(readonly=True)
    rows = st.catalog(asset=asset)
    if not rows:
        return None

    out = {"asset": asset, "source": source, "table": table,
           "columns": None, "primary_key": None, "foreign_keys": None,
           "observed_at": None, "inferred": [], "confirmed": [], "refuted": []}
    for r in rows:
        if r["kind"] == "schema" and r["status"] == "observed":
            if r["key"] == "columns":
                out["columns"] = r["value"]
                out["observed_at"] = r["observed_at"]
            elif r["key"] == "primary_key":
                out["primary_key"] = r["value"]
        elif r["kind"] == "foreign_keys" and r["status"] == "observed":
            out["foreign_keys"] = r["value"]
        elif r["status"] in ("inferred", "confirmed", "refuted"):
            out[r["status"]].append(r)
    if out["columns"] is None:
        return None                      # 只有推断没有结构，算无档
    out["stale_hours"] = (time.time() - out["observed_at"]) / 3600.0
    return out


def snapshot(source: str, table: str, refresh: bool = False) -> tuple:
    """有档读档，无档才采集。返回 `(快照, 是否碰过源库)`。

    第二个返回值不是给日志看的 —— 「这次答案有没有回源库」是闭环 A 的
    验收判据本身，得能被断言。
    """
    if not refresh:
        got = lookup(source, table)
        if got:
            return got, False
    return observe(source, table), True


def ownership(asset: str) -> dict:
    """这张表归谁。**区分人确认过的和按表名猜的。**

    确认过的归属存在 `asset_semantics` 的 `ownership` 键里（门禁的
    `resolve_approver_role` 也读它，两处同源）。没有确认就按表名前缀推 ——
    那只是约定，所以标成 inferred，不能说得像事实。
    """
    st = _store(readonly=True)
    try:
        known = st.known(asset, "ownership")
    except Exception:                                        # noqa: BLE001
        known = None
    if known and known.get("value"):
        role = str(known["value"]).strip()
        return {"role": role, "status": "confirmed",
                "by": known.get("confirmed_by") or "",
                "person": _holder(st, role)}

    table = asset.split(".")[-1]
    domain = table.split("_")[0].lower() if table else ""
    role = f"owner:{domain}" if domain else ""
    person = _holder(st, role) if role else ""
    if not person:
        return {"role": "", "status": "unknown", "by": "", "person": ""}
    return {"role": role, "status": "inferred", "by": "",
            "person": person, "evidence": f"表名前缀 {domain}（只是约定）"}


def _holder(st, role):
    if not role:
        return ""
    try:
        return st.resolve_role(role) or ""
    except Exception:                                        # noqa: BLE001
        return ""


def semantics(asset: str) -> list:
    """这张表（及其列）上的口径，按状态分好。

    口径当前值仍在 `asset_semantics`（那张表是门禁与清洗的读取点，
    不动它）；这里只负责把「谁定的」翻译成 confirmed / inferred。
    `system:` 开头的是机器推断，不是人确认过的 —— 这一句区分正是
    R6 要补的东西。
    """
    st = _store(readonly=True)
    out = []
    for a, k, v, by, at in _semantics_rows(st, asset):
        col = a[len(asset) + 1:] if a.startswith(asset + ".") else ""
        by = by or ""
        out.append({"key": f"{col}.{k}" if col else k, "value": v,
                    "status": "inferred" if by.startswith("system:") else "confirmed",
                    "by": by, "at": at})
    return out


def _semantics_rows(st, asset):
    """读 `asset_semantics`。**两个后端占位符不同**，各走各的。

    原先 `trace_asset` 用 `hasattr(st.db, "execute")` 分支 —— psycopg3 的
    Connection 也有 `.execute`，于是 Postgres 走进了 SQLite 那条路，
    `?` 占位符直接抛错，被外层吞成「读不到治理库」。按后端类名判。
    """
    sql = ("SELECT asset, key, value, confirmed_by, confirmed_at"
           " FROM asset_semantics WHERE asset = {0} OR asset LIKE {0}"
           " ORDER BY asset, key")
    try:
        if type(st).__name__ == "PgStore":
            with st.db.cursor() as c:
                c.execute(sql.format("%s"), (asset, asset + ".%"))
                return c.fetchall()
        return st.db.execute(sql.format("?"), (asset, asset + ".%")).fetchall()
    except Exception:                                        # noqa: BLE001
        return []


# ------------------------------------------------------------------ 写：推断
def record_inference(asset: str, kind: str, key: str, value, evidence,
                     actor: str = "system") -> int | None:
    """记一条推断。**evidence 必填** —— 没有依据的推断没法复核，
    下次也无从判断它为什么错。"""
    if not evidence:
        raise ValueError("推断必须带 evidence：凭什么这么说")
    return _store().catalog_put(asset, kind, key, value, "inferred",
                                actor, evidence=evidence)


def confirm(asset: str, kind: str, key: str, value, actor: str,
            evidence=None) -> int | None:
    """人确认。**不删推断**，只把它标成被取代的 —— 系统曾经猜过什么、
    人为什么改了它，都还查得到。"""
    return _store().catalog_put(asset, kind, key, value, "confirmed",
                                actor, evidence=evidence)


def allowed_values(asset: str) -> list | None:
    """这一列人确认过的取值范围。**没登记返回 None，不是空列表。**

    「没人定过枚举」和「人定过、枚举是空的」是两件事：前者不该校验，
    后者该全部报错。返回空列表会把前者悄悄变成后者。
    """
    # **精确匹配。** `catalog()` 按前缀查（要能一次拿到整张表连同各列），
    # 但取值范围是**某一列**的事 —— 前缀匹配会让问表名时随便返回某一列的
    # 枚举，那比返回 None 糟得多。
    for r in _store(readonly=True).catalog(asset=asset, kind="constraint"):
        if (r["asset"] == asset and r["key"] == "allowed_values"
                and isinstance(r["value"], list)):
            return r["value"]
    return None


def refute(asset: str, kind: str, key: str, reason: str, actor: str) -> int | None:
    """否定一条推断。**留着，别删** —— 免得下次再猜同一个错。"""
    return _store().catalog_put(asset, kind, key, reason, "refuted",
                                actor, evidence={"reason": reason})


# ------------------------------------------------------------------ 呈现
def _ts(t):
    return time.strftime("%m-%d %H:%M", time.localtime(float(t or 0)))


def _age(h):
    if h is None:
        return ""
    if h < 1:
        return f"{int(h * 60)} 分钟前"
    if h < 48:
        return f"{h:.1f} 小时前"
    return f"{h / 24:.1f} 天前"


LABEL = {"observed": "观测", "inferred": "推断", "confirmed": "已确认",
         "refuted": "已否定", "unknown": "未知"}


def render(snap: dict, hit_source: bool = False) -> str:
    """把快照写成给模型看的文本。**每一条都标出是观测、推断还是确认。**

    R5 实测过一次「查不到它就会猜」，猜出来的答案（把连接账号说成
    postgres 超级用户）比不答更糟。所以标注不是排版，是这个工具的正事。
    """
    asset = snap["asset"]
    cols = snap.get("columns") or []
    pk = snap.get("primary_key") or []
    L = []
    if hit_source:
        L.append(f"（**无档，刚刚采集了一次**：{asset}）")
    else:
        L.append(f"（读的是档案快照，采于 {_ts(snap['observed_at'])}，"
                 f"{_age(snap.get('stale_hours'))} —— **没有回源库**。"
                 f"要确认源库现在是不是这样，用 `refresh=true` 重新采集。）")

    L.append(f"\n## {asset}［观测］{len(cols)} 列"
             + (f"，主键 {'、'.join(pk)}" if pk else "，**没有主键**"))
    L.append("列：" + "、".join(
        f"{c[0]}:{c[1]}" + ("" if str(c[2]).upper() == "YES" else "*")
        for c in cols[:40]))
    L.append("（`*` = NOT NULL）")
    if len(cols) > 40:
        L.append(f"（还有 {len(cols) - 40} 列没列出）")

    fks = snap.get("foreign_keys")
    if fks:
        L.append("\n## 外键［观测］")
        for t, c, rt, rc in fks[:12]:
            L.append(f"- {t}.{c} → {rt}.{rc}")
    elif fks == []:
        L.append("\n## 外键［观测］源库没有声明（不代表没有关系，可能靠约定）")
    else:
        L.append("\n## 外键 **无档** —— 采集时没读到，别当成「没有外键」")

    own = ownership(asset)
    if own["status"] == "unknown":
        L.append("\n## 负责人 **无档** —— 没人说过这张表归谁。别猜，去问。")
    else:
        who = own["person"] or "（角色下没有在任的人）"
        L.append(f"\n## 负责人［{LABEL[own['status']]}］{own['role']} → {who}")
        if own["status"] == "confirmed":
            L.append(f"- {own['by']} 确认过")
        else:
            L.append(f"- 依据：{own.get('evidence', '')}。**没人确认过**，"
                     f"发审批前先问一句")

    enums = [r for r in (snap.get("confirmed") or [])
             if r["kind"] == "constraint" and r["key"] == "allowed_values"]
    if enums:
        L.append("\n## 取值范围（人确认过的枚举，清洗结果按它校验）")
        for r in enums:
            col = (r["asset"][len(asset) + 1:] if r["asset"].startswith(asset + ".")
                   else r["asset"])
            L.append(f"- `{col}`［已确认］{'、'.join(map(str, r['value']))}"
                     f" —— {r['actor']}（{_ts(r['observed_at'])}）")

    sem = semantics(asset)
    if sem:
        L.append("\n## 口径")
        for s in sorted(sem, key=lambda x: x["key"]):
            L.append(f"- `{s['key']}`［{LABEL.get(s['status'], s['status'])}］"
                     f"{str(s['value'])[:120]} —— {s['by']}（{_ts(s['at'])}）")
    else:
        L.append("\n## 口径 **无档** —— 没有人定过这张表的口径")

    extra = [r for r in (snap.get("inferred") or []) + (snap.get("refuted") or [])
             if r["kind"] not in SCHEMA_KINDS]
    if extra:
        L.append("\n## 其它推断")
        for r in extra:
            L.append(f"- [{r['kind']}/{r['key']}]［{LABEL[r['status']]}］"
                     f"{json.dumps(r['value'], ensure_ascii=False)[:120]}"
                     f"\n  依据：{json.dumps(r['evidence'], ensure_ascii=False)[:120]}")

    L.append("\n> ［观测］= 源库里读到的　［推断］= 猜的，待验证　"
             "［已确认］= 人拍过板。**标着推断的别当结论用。**")
    return "\n".join(L)
