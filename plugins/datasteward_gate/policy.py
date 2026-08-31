"""工具策略表：限制是配置，不是代码分支（readme 4.0）。

新增一条限制 = 在这里加一行，不改 hook 代码。
"""
from enum import IntEnum


class Level(IntEnum):
    L0 = 0  # 读元数据、profiling —— 自由执行，不通知
    L1 = 1  # 落 bronze、跑 DQ —— 自由执行，记入周报
    L2 = 2  # 口径定义、脏数据处理 —— 需 Steward 确认
    L3 = 3  # 接入源、发布 gold、开权限 —— 需 Owner 审批
    L4 = 4  # 改源系统、删已发布数据 —— 永不自动


# tool_name -> (level, approver_role)
POLICY: dict[str, tuple[Level, str | None]] = {
    # --- L0 发现 ---
    "get_table_metadata":   (Level.L0, None),
    "search_asset":         (Level.L0, None),
    "get_lineage":          (Level.L0, None),
    "list_source_tables":   (Level.L0, None),
    # --- L1 常规作业 ---
    "profile_table":        (Level.L1, None),
    "run_dq_check":         (Level.L1, None),
    "sql_query":            (Level.L1, None),
    # --- L2 需 Steward 确认 ---
    "define_semantics":     (Level.L2, "steward"),
    "apply_cleaning_rule":  (Level.L2, "steward"),
    # --- L3 需 Owner 审批 ---
    "connect_source":       (Level.L3, "owner"),
    "ingest_table":         (Level.L3, "owner"),
    "publish_gold":         (Level.L3, "owner"),
    "grant_read":           (Level.L3, "owner"),
    "full_refresh":         (Level.L3, "owner"),
    # --- L4 永不自动 ---
    "grant_write":          (Level.L4, None),
    "drop_source_table":    (Level.L4, None),
    "delete_published":     (Level.L4, None),
    "unmask_pii":           (Level.L4, None),

    # --- L4：Agent 不得改动自己的权限（readme 11.6）---
    # 没有这一组，前面所有门禁都有一个后门：
    # Agent 可以先给自己提权，再去做别的事。
    "modify_own_role":      (Level.L4, None),
    "grant_self":           (Level.L4, None),
    "modify_role_assignment": (Level.L4, None),
    "alter_access_policy":  (Level.L4, None),
    "rotate_own_credential": (Level.L4, None),
}

# 未在表中声明的工具的默认级别。
# 保守取 L2：新工具默认需要人确认，而不是默认放行。
DEFAULT = (Level.L2, "owner")


def lookup(tool_name: str) -> tuple[Level, str | None]:
    return POLICY.get(tool_name, DEFAULT)
