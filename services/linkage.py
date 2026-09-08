"""跨系统实体对应：**这两张表凭什么能连**（闭环 C）。

源库自己声明的外键，`catalog` 已经存了（observed 层）。但**跨系统没有外键** ——
两个 `customer_id` 是不是同一个人，源库回答不了，只有业务知道。

所以这条链是三段，一段都不能省：

    候选发现（模型/规则推断）  →  人确认（L2）  →  用它回答问题
       落 inferred，带证据          落 confirmed        记依赖的数据版本

三条硬规矩：

1. **候选发现只在 lake 侧做。** 值域重叠、基数统计都是重扫描，
   打在源库上会把它拖死（铁律 3）。
2. **证据必须能被人复核。** 「重叠率 87%」比「看起来像」有用得多 ——
   人要据此判断，给不出证据的候选就是在让人替模型背书。
3. **被否定的假设留着**（refuted + 原因）。下次再想连同一对表时，
   先看这里 —— 「上次为什么说不行」比重新猜一遍便宜。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "services"), str(ROOT / "plugins")]

# 只看这些类型的列 —— 实体标识不会是浮点数或时间戳。
# 按**族**分：文本对文本、整数对整数。不同族不配对 ——
# `overlap()` 里两边都 `CAST AS VARCHAR`，7 和 '7' 会看着能连，那是假重叠。
TYPE_FAMILIES = {
    "text": ("char", "text", "varchar", "uuid"),
    "int": ("int", "bigint", "smallint"),
}
ID_LIKE = tuple(t for fam in TYPE_FAMILIES.values() for t in fam)
# 一轮候选发现最多探多少对列。**这是钱的上限，不是判断的上限** ——
# 每对要 6 次 Trino 查询，N×M 对全探会把 `-Xmx1G` 的 Trino 拖垮。
# 超预算的对不会被丢掉，会原样报给模型（`unprobed`），由它指名再探。
# 单对 ~1.6s（合并查询之后），24 对约 38s —— 一次工具调用等得起。
# 调大用 `LINKAGE_MAX_PROBE`；宽表想全探就分几次，用 probe_pairs 指名。
MAX_PROBE = int(__import__("os").environ.get("LINKAGE_MAX_PROBE", "24"))
# 采样上限：候选发现是探查，不是全量核对。
SAMPLE = 50000
# 表行数按表缓存：一次候选发现里同一张表要被问很多遍，
# 而 `count(*)` 每次都是一次全表扫。
# **每轮 `candidates()` 开头清空** —— agent 进程是长驻的，
# 留着跨轮用就成了「拿旧观测当新事实」，正是闭环 B 要防的那件事。
_ROWS: dict = {}


def _q(sql):
    import sync
    return sync._trino(sql)


def _one(sql):
    r = _q(sql)
    return r[0] if r else None


def _bronze(asset: str) -> str:
    """`源.表` → bronze 里的表名。"""
    src, tbl = asset.split(".", 1)
    return f"{src}__{tbl}"


def _columns(asset: str) -> list:
    """从**档案**里读列（不回源库）。档案没有就报错 —— 先建档再连表。"""
    import catalog
    src, tbl = asset.split(".", 1)
    snap = catalog.lookup(src, tbl)
    if not snap or not snap.get("columns"):
        raise ValueError(f"{asset} 没有建档 —— 先 observe 再谈连表")
    return snap["columns"]


def _type_family(t) -> str:
    """列类型 → 族名；不是实体标识的类型返回空串。"""
    low = str(t).lower()
    for fam, toks in TYPE_FAMILIES.items():
        if any(tok in low for tok in toks):
            return fam
    return ""


def _norm(name: str) -> str:
    """列名归一，用来比相似度。

    `customer_id` / `customerid` / `cust_id` 在人看来是一回事，
    但**归一只用来挑候选，不用来下结论** —— 名字像不代表指同一个东西。
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _name_score(a: str, b: str) -> float:
    x, y = _norm(a), _norm(b)
    if x == y:
        return 1.0
    if x in y or y in x:
        return 0.8
    # 公共后缀（customer_id vs unique_id 都以 id 结尾）：弱信号
    import difflib
    return round(difflib.SequenceMatcher(None, x, y).ratio(), 2)


def overlap(asset_a: str, col_a: str, asset_b: str, col_b: str) -> dict:
    """两列的值域关系 —— **候选的硬证据，在 lake 侧算**。

    只报重叠率是不够的。实测撞到过：`crm_customer.customer_id` 与
    `crm_contact.customer_id`（对的）和与 `crm_contact.id`（错的）
    **重叠率都是 1.0** —— 因为 id 1..55 完整覆盖 customer_id 1..40。
    单看重叠率，两条候选一模一样。

    分得开它们的是另外两个量：

    - **右侧唯一性**：`id` 是主键、天然唯一，那这条连接只能是 1:1；
      但 55 条联系人挂 40 个客户，真正的关系必然是 N:1 —— 矛盾。
    - **孤儿行**：按 `id` 连，41..55 这 15 条联系人找不到客户；
      按 `customer_id` 连，一条都不孤。

    三个量一起摆给人看。**它们仍然不下结论** —— 下结论的是人。
    """
    ta, tb = _bronze(asset_a), _bronze(asset_b)
    da = (f'SELECT DISTINCT CAST("{col_a}" AS VARCHAR) v'
          f' FROM iceberg.bronze."{ta}" WHERE "{col_a}" IS NOT NULL'
          f' LIMIT {SAMPLE}')
    db = (f'SELECT DISTINCT CAST("{col_b}" AS VARCHAR) v'
          f' FROM iceberg.bronze."{tb}" WHERE "{col_b}" IS NOT NULL'
          f' LIMIT {SAMPLE}')
    # **五个量一次查完。** 原先是 5 次独立往返，实测单对 7.58s ——
    # 而候选发现要探几十对，那个成本高到只能靠「名字不像就不探」来压，
    # 于是一道判定被伪装成了成本过滤（见 `candidates` 的注释）。
    # 合并成一条 CTE 之后单对 1.6s，预算才谈得上放宽。
    #
    # 孤儿数算在**已去重的集合**上（EXCEPT），不是 `NOT IN` 扫全表。
    # 后者在 10 万行的表上会把 `-Xmx1G` 的 Trino 拖到查询失败 —— 实测撞过：
    # 单跑没事，全量回归并发时这一条就报 SyncError。
    # 代价换的是精度：得到的是「多少个**值**连不上」，不是「多少**行**」。
    # 对判断能不能连，值够用；要按行加权时再说。
    row = _one(
        f"WITH a AS ({da}), b AS ({db}),"
        f" ca AS (SELECT count(*) n FROM a), cb AS (SELECT count(*) n FROM b),"
        f" ci AS (SELECT count(*) n FROM (SELECT v FROM a INTERSECT SELECT v FROM b)),"
        f" oa AS (SELECT count(*) n FROM (SELECT v FROM a EXCEPT SELECT v FROM b)),"
        f" ob AS (SELECT count(*) n FROM (SELECT v FROM b EXCEPT SELECT v FROM a))"
        f" SELECT ca.n, cb.n, ci.n, oa.n, ob.n FROM ca, cb, ci, oa, ob")
    got = [int(x) for x in str(row or "0,0,0,0,0").split(",")]
    na, nb, shared, orphan_a, orphan_b = (got + [0] * 5)[:5]
    out = {"a_distinct": na, "b_distinct": nb, "shared": 0, "rate": 0.0}
    if not na or not nb:
        return out
    ra, rb = _rows_of(ta), _rows_of(tb)
    out.update({
        "shared": shared, "rate": round(shared / min(na, nb), 4),
        "a_rows": ra, "b_rows": rb,
        "a_unique": na == ra, "b_unique": nb == rb,
        "a_orphans": orphan_a, "b_orphans": orphan_b,
        "cardinality": _cardinality(na == ra, nb == rb),
    })
    return out


def _rows_of(table: str) -> int:
    if table not in _ROWS:
        _ROWS[table] = int(_one(f'SELECT count(*) FROM iceberg.bronze."{table}"') or 0)
    return _ROWS[table]


def _cardinality(a_unique: bool, b_unique: bool) -> str:
    """两侧唯一性 → 这条连接的形状。人一眼能看出它跟业务对不对得上。"""
    if a_unique and b_unique:
        return "1:1"
    if a_unique:
        return "1:N"
    if b_unique:
        return "N:1"
    return "N:N"


def candidates(asset_a: str, asset_b: str, min_rate: float = 0.05,
               budget: int = MAX_PROBE, pairs_only=None) -> dict:
    """找出这两张表之间可能的连接列。

    **产出候选，不产出结论。** 每条都带证据（去重基数、值域重叠率、
    连接形状、孤儿值），排序只是方便人先看最像的那条。

    ## 名字相似度只排序，不排除

    早先这里有一道 `name_score < 0.5 就跳过`。它省钱，但它**是一次判定
    伪装成的成本过滤** —— `买家编号` vs `buyer_id`、`cust_no` vs
    `customer_id` 都过不了 0.5，而跨系统对应最常见的形状恰恰就是这种。
    交接里这一步写的是「模型推断」，用字符串相似度替掉，等于把需要业务
    语义的判断交给了 `difflib`。

    现在的形状（CLAUDE.md「形态先定」）：

      确定性粗筛（便宜、宁可多给）  →  模型判定（带证据）
        类型兼容 + 探查预算              propose_link 把证据交给模型

    预算的作用是「这一轮只探得起这么多对」，不是「这几对我觉得不像」。
    **没探到的对要报出来**（返回值的 `unprobed`），模型看了觉得该探，
    用 `pairs_only` 指名再来一次 —— 名字不像但确实对得上的那条，
    出口在这里。

    返回 `{"pairs": [...], "unprobed": [...], "dropped": n, "budget": n}`。
    """
    _ROWS.clear()                    # 见 `_ROWS` 的注释：不跨轮复用
    ca, cb = _columns(asset_a), _columns(asset_b)
    want = {(x.strip(), y.strip()) for x, y in (pairs_only or [])}

    # ---- 确定性粗筛：类型兼容。**这一层不看名字。** ----
    # 实体标识不会是浮点数或时间戳；文本和数字也不该硬凑一对
    # （`CAST AS VARCHAR` 会让 7 和 '7' 看着能连，那是假的重叠）。
    plan = []
    for a in ca:
        fa = _type_family(a[1])
        if not fa:
            continue
        for b in cb:
            if _type_family(b[1]) != fa:
                continue
            if want and (a[0], b[0]) not in want:
                continue
            plan.append((a[0], b[0], _name_score(a[0], b[0])))

    # 名字相似度在这里**只决定先探谁**，不决定探不探。
    plan.sort(key=lambda t: -t[2])
    probe = plan if want else plan[:max(0, budget)]
    unprobed = [] if want else plan[max(0, budget):]

    pairs, dropped = [], 0
    for col_a, col_b, ns in probe:
        try:
            ov = overlap(asset_a, col_a, asset_b, col_b)
        except Exception as e:                                # noqa: BLE001
            pairs.append({"a": col_a, "b": col_b, "name_score": ns,
                          "error": f"{type(e).__name__}: {str(e)[:60]}"})
            continue
        if ov["rate"] < min_rate:
            # 值域几乎不重叠 —— 这是**观测到的证据**，不是我的看法，
            # 所以可以丢。但要报个数，别让「探过 40 对只剩 2 条」
            # 看起来像「只有 2 对值得看」。
            dropped += 1
            continue
        pairs.append({"a": col_a, "b": col_b, "name_score": ns, **ov,
                      "note": _note(ov)})
    # 排序先看孤儿值（少的在前），再看重叠率、再看名字。
    # **排序不是判定** —— 它只决定人先看哪条。真正把 `id` 那条候选
    # 排到后面去的是「15 个值连不上」，不是「我觉得它不对」。
    pairs.sort(key=lambda x: (x.get("a_orphans", 9 ** 9) + x.get("b_orphans", 9 ** 9),
                              -x.get("rate", 0), -x["name_score"]))
    return {"pairs": pairs,
            "unprobed": [{"a": x, "b": y, "name_score": n} for x, y, n in unprobed],
            "dropped": dropped, "probed": len(probe), "planned": len(plan)}


def _note(ov: dict) -> str:
    """把证据翻成人话。**只描述，不建议** —— 该不该连是人的判断。"""
    bits = []
    if ov.get("b_orphans"):
        bits.append(f"右表有 {ov['b_orphans']} 个值连不上左表任何一行")
    if ov.get("a_orphans"):
        bits.append(f"左表有 {ov['a_orphans']} 个值连不上右表任何一行")
    if ov.get("cardinality"):
        bits.append(f"形状 {ov['cardinality']}")
    if not ov.get("a_orphans") and not ov.get("b_orphans"):
        bits.append("两侧都不孤")
    return "；".join(bits)


def propose(asset_a: str, asset_b: str, col_a: str, col_b: str,
            evidence: dict, actor: str = "model") -> dict:
    """把一条候选落进档案的 **inferred** 层。

    键用「对端资产 + 两个列」拼，这样同两张表之间可以有多条候选并存，
    而重复提议同一条不会开出第二行（`catalog_put` 按内容判重）。
    """
    import catalog
    key = f"{asset_b}:{col_a}={col_b}"
    val = {"target": asset_b, "left": col_a, "right": col_b}
    rid = catalog.record_inference(asset_a, "link", key, val, evidence,
                                   actor=actor)
    return {"asset": asset_a, "key": key, "row": rid, "status": "inferred"}


def confirm(asset_a: str, key: str, actor: str, note: str = "") -> dict:
    """人确认了这条对应关系 —— 从 inferred 升成 **confirmed**。"""
    import catalog
    cur = _find(asset_a, key)
    if not cur:
        return {"error": f"{asset_a} 上没有 {key} 这条候选"}
    val = dict(cur["value"] or {})
    val["confirmed_note"] = note[:300]
    rid = catalog.confirm(asset_a, "link", key, val, actor)
    return {"asset": asset_a, "key": key, "row": rid, "status": "confirmed"}


def refute(asset_a: str, key: str, actor: str, reason: str) -> dict:
    """人否定了这条候选。**留着不删** —— 下次再猜同一对表时先看这里。"""
    import catalog
    rid = catalog.refute(asset_a, "link", key, reason, actor)
    return {"asset": asset_a, "key": key, "row": rid, "status": "refuted",
            "reason": reason}


def _find(asset: str, key: str):
    from datasteward_gate.approvals import open_store
    with open_store(readonly=True, init_schema=False) as st:
        for r in st.catalog(asset, kind="link", limit=200):
            if r["key"] == key and r["asset"] == asset:
                return r
    return None


def links(asset: str, status: str | None = None) -> list:
    """这张表已有的连接：候选 / 已确认 / 已否定。"""
    from datasteward_gate.approvals import open_store
    with open_store(readonly=True, init_schema=False) as st:
        rows = [r for r in st.catalog(asset, kind="link", limit=200)
                if r["asset"] == asset]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


# ------------------------------------------------------------------ 用它回答
def data_versions(assets: list) -> dict:
    """这些资产**此刻**的数据版本 —— 结论要记它，否则源变了还以为结论有效。

    两个来源，按可信度排：

    1. `sync_state`：最后同步时刻 + 行数 + 结构指纹。这是**接入方**的说法。
    2. 湖里现查的行数：这是**被查的那份数据自己**的说法。

    两个都取。它们对不上本身就是信号 —— 台账说 600 行、湖里 40 行，
    说明中间发生过什么没记上。只记一个就看不见这件事。

    读取走 `sync.get_state` —— **不要在这里自己判后端**。
    `hasattr(st.db, "execute")` 那种写法 psycopg3 也满足，
    这个项目已经为它翻过一次车（见 `catalog._semantics_rows` 的注释）。
    """
    import sync
    out = {}
    for a in assets:
        v = {"synced_at": None, "rows": None, "schema_hash": None}
        stt = sync.get_state(a)
        if stt:
            v.update({"synced_at": stt.get("last_synced_at"),
                      "rows": stt.get("row_count"),
                      "schema_hash": stt.get("schema_hash")})
        try:
            v["lake_rows"] = int(_one(
                f'SELECT count(*) FROM iceberg.bronze."{_bronze(a)}"') or 0)
        except Exception:                                     # noqa: BLE001
            v["lake_rows"] = None
        if v["rows"] is not None and v["lake_rows"] is not None \
                and int(v["rows"]) != v["lake_rows"]:
            # **不要悄悄挑一个信。** 台账与湖对不上时把两个都摆出来。
            v["mismatch"] = f'台账记 {v["rows"]} 行，湖里现查 {v["lake_rows"]} 行'
        if v["synced_at"] is None and v["lake_rows"] is None:
            v["note"] = "既没有同步记录、湖里也查不到 —— 这份数据的来历不明"
        elif v["synced_at"] is None:
            v["note"] = "没有同步记录，版本只能按湖里现查的行数算"
        out[a] = v
    return out


def answer(question: str, sql: str, assets: list, actor: str = "",
           verify_sql: str | None = None, expect=None) -> dict:
    """用已确认的连接回答一个问题，**并记下它依赖了哪一份数据**。

    `verify_sql` / `expect`：换一条路径算同一个数。**答案对不对，
    不能只看 SQL 跑通了** —— 跑通只说明语法对，join 连错了照样有结果。
    核验不通过时如实标出来，不把结果吞掉。
    """
    versions = data_versions(assets)
    try:
        rows = _q(sql)
    except Exception as e:                                    # noqa: BLE001
        return {"question": question, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    result = rows[:50]
    verdict = {"checked": False}
    if verify_sql:
        try:
            v = _q(verify_sql)
            got = v[0] if v else None
            verdict = {"checked": True, "via": "second_path", "value": got,
                       "expect": expect,
                       "ok": (str(got) == str(expect)) if expect is not None
                       else None}
        except Exception as e:                                # noqa: BLE001
            verdict = {"checked": False,
                       "error": f"{type(e).__name__}: {str(e)[:90]}"}

    rec = {"question": question, "sql": sql, "rows": len(rows),
           "result": result, "depends_on": versions, "verification": verdict}
    # 结论进台账 —— 它是一次「基于这些数据得出的判断」，跟接入、清洗一样
    # 属于资产的经历。哪天源变了，回头能查出「那个结论是基于哪一份数据的」。
    try:
        from datasteward_gate.approvals import open_store
        with open_store(readonly=False, init_schema=True) as st:
            for a in assets:
                st.record_provenance(a, "answered", actor=actor or "model",
                                     detail={"question": question[:200],
                                             "rows": len(rows),
                                             "result": result[:50],
                                             "depends_on": versions,
                                             "verification": verdict})
    except Exception:                                         # noqa: BLE001
        pass
    return rec
