"""审批身份的归一 —— **模型自由生成的参数不能直接当身份**。

## 这是在修什么

live eval 实测（R6 §13）：票批的是
`define_semantics(asset="acme.fin_invoice", key="enum_rule")`，
模型回来调的是 `key="normalize_rule"`、另一次是
`asset="acme.fin_invoice.status"`。`IDENTITY_KEYS` 里这个工具的身份就是
`(asset, key)` —— 指纹对不上，**人批过的那张票白批了，又开一张新的**。

R5 撞过两次同一形状（dsn、description），但那两次变的是**修饰字段**，
用 `IDENTITY_KEYS` 挑出身份字段就解决了。这次变的是**身份字段本身**：
`key` 是模型自由填的自然语言，`asset` 的粒度（表级还是列级）也没约束。
挑字段挑不出来 —— 得先把值本身归一。

## 边界：归一只喂指纹，不改要执行的参数

```
模型给的原始参数
   → normalize_approval_args()   （确定性规则，这个文件）
   → identity 元组 → action_hash → 票据 / 去重 / 索引
   → 人批准
   → 恢复时 gate 回填**票里那份原始参数**去执行
```

**执行的永远是人看过的那一份**（`_replay_approved_args`），归一只负责让
「同一件事」算出同一个指纹。所以归一错了最坏是多发一张审批（吵），
不会让两件不同的事共用一张票（松）—— 与 `IDENTITY_KEYS` 同一条取舍：
**宁可吵，不可松。**

## 为什么不做成模型可调的工具

做成工具 = 模型得记得调它。归一是**授权身份**的一部分（执行边界 1），
必须每次都发生，所以放在 `action_hash` 这条唯一入口上，由代码调。

## 只收敛显式列出的同义词

**不认识的 key 保持原样**（打上 `?` 前缀标记未归一），不硬塞进某个类别。
猜错的代价是两件不同的事共用一张票 —— 那正是上面说的「松」。
加一个同义词 = 在 `_KEY_SYNONYMS` 里加一行。
"""
import re

# ---------------------------------------------------------------------------
# 受控词表：口径的**类别**
#
# 取自仓库里实际用过的 key（`ownership` / `normalize_rule` / `null_meaning` /
# `enum_rule` / `region_rule` / `unit_rule` / `revenue_definition` /
# `export_column_mapping`）加上工具 schema 里举的例子，不是凭空设计的。
#
# **同一格里的都收敛到同一个 canonical。** `enum_rule` 与 `normalize_rule`
# 合并是这次实测直接要求的：「status 只有这四个取值，统一成全大写」
# 是一条口径，不是两条。
# ---------------------------------------------------------------------------
CANONICAL_KEYS = {
    "null_meaning":   "空值是什么意思",
    "value_domain":   "取值范围与归一（枚举、大小写、空格）",
    "unit":           "单位与量级",
    "derivation":     "这一列按什么填 / 怎么算出来",
    "grain":          "粒度",
    "deprecated":     "已废弃",
    "ownership":      "归属",           # 门禁里已经当常量在用
    "column_mapping": "导出列名到字段的映射",
}

_KEY_SYNONYMS = {
    # null
    "null_meaning": "null_meaning", "null_rule": "null_meaning",
    "nulls": "null_meaning", "empty_meaning": "null_meaning",
    "null_semantics": "null_meaning",
    # 取值范围 / 归一
    "value_domain": "value_domain", "enum_rule": "value_domain",
    "normalize_rule": "value_domain", "enum": "value_domain",
    "allowed_values": "value_domain", "value_rule": "value_domain",
    "normalization": "value_domain", "case_rule": "value_domain",
    "valueset": "value_domain", "value_set": "value_domain",
    # 下面这几个是 live 实测里模型真的写出来的形式（`enum_values`、
    # `status_values`）。**加同义词是治标**：治本是把词表告诉模型
    # （工具 schema 里那段 `vocabulary_hint()`），让它一开始就从这几个里挑。
    "enum_values": "value_domain", "allowed_value": "value_domain",
    "allowed": "value_domain", "domain": "value_domain",
    # 单位
    "unit": "unit", "unit_rule": "unit", "units": "unit", "scale": "unit",
    # 怎么填 / 怎么算
    "derivation": "derivation", "calc_rule": "derivation",
    "definition": "derivation", "revenue_definition": "derivation",
    "source_rule": "derivation", "fill_rule": "derivation",
    # 粒度
    "grain": "grain", "granularity": "grain", "grain_rule": "grain",
    # 废弃
    "deprecated": "deprecated", "deprecation": "deprecated",
    "obsolete": "deprecated", "retired": "deprecated",
    # 归属
    "ownership": "ownership", "owner": "ownership", "owner_role": "ownership",
    "responsible": "ownership",
    # 列映射
    "column_mapping": "column_mapping",
    "export_column_mapping": "column_mapping", "mapping": "column_mapping",
}

_SLUG = re.compile(r"[^a-z0-9]+")


def canonical_key(raw) -> tuple:
    """(canonical, unresolved)。**认不出来就原样保留并标记**，不硬归类。"""
    slug = _SLUG.sub("_", str(raw or "").strip().lower()).strip("_")
    if not slug:
        return "", False
    hit = _KEY_SYNONYMS.get(slug)
    if hit:
        return hit, False
    return f"?{slug}", True


def split_asset(raw) -> tuple:
    """`acme.fin_invoice.status` → (`acme.fin_invoice`, `status`)。

    **按本项目的资产命名约定切**：资产是 `source.table`，所以第三段起
    就是列。两段及以下原样保留，`target` 留空 —— 宁可不切，
    不要把 `orders.customer_type` 猜成表加列。

    没有做 schema 校验（不查档案确认那一段真是列名）：查档案要连库，
    而 `action_hash` 在门禁最热的那条路上，且离线也得算得出来。
    代价是列名写错时切出一个不存在的列 —— 那仍然是**确定性**的同一个
    结果，指纹照样稳定，只是人在票里看到的对象名有错。
    """
    s = str(raw or "").strip()
    parts = [p for p in s.split(".") if p]
    if len(parts) >= 3:
        return ".".join(parts[:-1]), parts[-1]
    return s, ""


# tool_name -> 从原始参数算出身份三元组的规则。
# **不声明的工具走 IDENTITY_KEYS 原有那条路**，行为不变。
def _define_semantics(args: dict) -> dict:
    asset, target = split_asset(args.get("asset"))
    key, unresolved = canonical_key(args.get("key"))
    # 列既可能写在 asset 里（`acme.t.col`），也可能是显式参数
    target = target or str(args.get("column") or "").strip()
    return {"identity": {"asset": asset, "target": target, "key": key},
            "unresolved": ["key"] if unresolved else []}


RULES = {
    "define_semantics": _define_semantics,
}


def normalize_approval_args(tool_name: str, args: dict) -> dict:
    """把模型给的原始参数变成**稳定的身份**。

    返回 `{"identity": {...}|None, "unresolved": [...]}`。
    `identity` 为 None = 这个工具没有归一规则，调用方走原来那条路。

    纯函数：不连库、不读环境、不点模型。判分与迁移脚本都能离线复算。
    """
    rule = RULES.get(tool_name)
    if rule is None:
        return {"identity": None, "unresolved": []}
    return rule(dict(args or {}))


def vocabulary_hint() -> str:
    """给模型看的受控词表。

    **这是治本那一半。** 同义词表只能追认模型已经写出来的形式，
    追不完 —— live 实测两轮就冒出 `enum_values` / `status_values` 两个新的。
    把可选值直接写进工具 schema，模型一开始就从这几个里挑，
    收敛问题的位置就从「事后归一」挪到了「提出时」。
    """
    return "、".join(f"`{k}`（{v}）" for k, v in CANONICAL_KEYS.items())


def asset_of(args: dict) -> str:
    """这次调用说的是**哪张表**（不含列）。找线、比对象都用它。

    `define_semantics` 给 `asset`，接入/清洗类给 `source` + `table` ——
    两种写法归到同一个串，否则「同一条线」在不同工具间对不上。
    """
    a = (args or {}).get("asset")
    if a:
        return split_asset(a)[0]
    src = str((args or {}).get("source") or (args or {}).get("source_id") or "").strip()
    tbl = str((args or {}).get("table") or "").strip()
    return f"{src}.{tbl}" if src and tbl else (tbl or src)
