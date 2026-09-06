"""记忆（readme 4.6 支柱三）。

两类，都很具体：

1. **资产关联** —— 哪张表关联哪张。一旦确认就不会变，
   却在每次分析多表关系时被重新推断
2. **审批偏好** —— 谁批得快、谁总拒某类、待办积压多少。
   让交互从「一个模板发所有人」变成「按人调整」

**两类都不需要新采集**：关联从源库元数据推断，偏好从已有的
approvals / decisions 统计。偏好与尝试计数复用 `asset_semantics`；
关联从 R6 起落 `asset_catalog` 的 inferred 层 —— 推断和人确认必须
分行存，否则人一确认就把推断覆盖掉。

> 记忆的价值不在存，在**取回时机**：在 Agent 准备提问之前先查一次，
> 命中就不问。否则沉淀了也等于没有。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _store(readonly=False):
    from plugins.datasteward_gate.approvals import open_store
    return open_store(readonly=readonly, init_schema=False)


# ---------------------------------------------------------------- 资产关联
def infer_asset_links(source_id: str) -> dict:
    """从源库元数据推断表间关系，落档案的 **inferred** 层。

    只用**确定性信号**（外键约束），不猜。命名相似之类的启发式
    留给模型判断——那是需要业务语义的部分（4.5）。
    """
    import connector
    fks = connector.list_foreign_keys(source_id)
    links = {}
    for tbl, col, ref_tbl, ref_col in fks:
        links.setdefault(tbl, []).append(
            {"to": ref_tbl, "via": f"{col} -> {ref_col}", "type": "foreign_key"})
        links.setdefault(ref_tbl, []).append(
            {"to": tbl, "via": f"{ref_col} <- {col}", "type": "referenced_by"})

    # **落 inferred 层，不落 `asset_semantics`。**
    #
    # 原先靠 `confirmed_by="system:fk_inference"` 这个字符串前缀跟人工确认
    # 区分，而 `UNIQUE(asset, key)` 意味着人一确认就把推断**覆盖掉** ——
    # 之后再也看不出「系统曾经推断过什么、人为什么改了它」，而那正是
    # 下次少犯错的依据（R6 闭环 A）。现在两者各自成行。
    import catalog
    for tbl, ls in links.items():
        catalog.record_inference(
            f"{source_id}.{tbl}", "link", "fk_derived", ls,
            evidence={"via": "pg_constraint 声明的外键", "source": source_id},
            actor="system:fk_inference")
    return {"source": source_id, "tables_with_links": len(links),
            "total_links": sum(len(v) for v in links.values())}


def related(source_id: str, table: str) -> list:
    """取回关联。**Agent 推断多表关系前先调这个**——命中就不用推断。

    先读档案的 inferred 层；读不到再回 `asset_semantics` —— R6 之前的
    存量库把它写在那里，升级不该让旧库的关联凭空消失。
    """
    import catalog
    asset = f"{source_id}.{table}"
    for r in catalog._store(readonly=True).catalog(asset=asset, kind="link"):
        if r["key"] == "fk_derived" and isinstance(r["value"], list):
            return r["value"]
    k = _store(readonly=True).known(asset, "links")
    if not k:
        return []
    try:
        return json.loads(k["value"])
    except Exception:
        return []


# ---------------------------------------------------------------- 审批偏好
def approver_profile(role: str) -> dict:
    """审批偏好。数据来自已有账本，不新增采集。"""
    st = _store(readonly=True)
    s = st.approver_stats(role)
    hints = []
    if s["avg_response_hours"] is not None:
        if s["avg_response_hours"] > 48:
            hints.append("响应偏慢，超时阈值可放宽，并提前准备升级路径")
        elif s["avg_response_hours"] < 4:
            hints.append("响应很快，可以放心多问")
    if s["approve_rate"] is not None and s["approve_rate"] < 0.5:
        hints.append("拒绝率高，提案前先补充依据或换方案")
    if s["pending"] >= 3:
        hints.append("待办已积压，暂缓新事项（WIP 限制会拦）")
    if s["abandoned"] > 0:
        hints.append(f"历史上有 {s['abandoned']} 件被放弃，考虑换人或降低打扰频率")
    return {**s, "hints": hints}


def suggest_evidence_level(role: str) -> str:
    """给这个人的提问该附多少证据。

    这就是「按人调整」的落点——Steward Playbook 的
    「告诉他什么会坏、你会怎么做」对不同的人分量不同。
    """
    p = approver_profile(role)
    if p["approve_rate"] is not None and p["approve_rate"] < 0.5:
        return "detailed"      # 拒得多 → 多给依据
    if p["avg_response_hours"] is not None and p["avg_response_hours"] < 4:
        return "brief"         # 回得快 → 从简
    return "standard"


# ---------------------------------------------------------------- 迭代上限
MAX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))


def record_attempt(asset: str, issue: str) -> int:
    """记一次修复尝试，返回累计次数。

    复用 `asset_semantics`，不加表——key 形如 `attempts:high_null_rate`。
    """
    st = _store()
    k = f"attempts:{issue}"
    cur = st.known(asset, k)
    n = int(cur["value"]) + 1 if cur else 1
    st.remember(asset, k, str(n), "system:iteration")
    return n


def attempts(asset: str, issue: str) -> int:
    st = _store(readonly=True)
    k = st.known(asset, f"attempts:{issue}")
    return int(k["value"]) if k else 0


def is_exhausted(asset: str, issue: str) -> bool:
    """是否已用完修复配额（readme 5.3）。

    > 3 轮还没解决，说明是业务规则问题而非技术问题，
    > 继续循环只是烧钱。

    用完后应标记 `needs_human_review` 并**退出自动处理**，
    而不是无限重试。
    """
    return attempts(asset, issue) >= MAX_ATTEMPTS


def exhausted_issues(asset: str) -> list:
    """该资产上所有已用尽配额的问题——进周报的「转人工」清单。"""
    st = _store(readonly=True)
    out = []
    for issue in ("high_null_rate", "primary_key_not_unique", "constant_column",
                  "format_inconsistent", "fk_broken"):
        n = attempts(asset, issue)
        if n >= MAX_ATTEMPTS:
            out.append({"issue": issue, "attempts": n})
    return out
