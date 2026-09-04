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
    # 只产出结论、不改任何东西：提案本身不需要审批，
    # 真正要审批的是「按这条规则去洗」（apply_cleaning_rule，L2）
    "propose_cleaning":     (Level.L1, None),
    # 写的是我们自己的整改台账，不碰源系统也不碰 lake
    "record_finding":       (Level.L1, None),
    # 权限扫描只读 information_schema / pg_roles，且明确不改任何权限（铁律 4）
    "scan_permissions":     (Level.L1, None),
    # 在自己的地盘上全量扫，扫爆了也不影响别人（readme 5.3）
    "check_lake_quality":   (Level.L1, None),
    "check_freshness":      (Level.L0, None),
    # --- L2 需 Steward 确认 ---
    "define_semantics":     (Level.L2, "steward"),
    "apply_cleaning_rule":  (Level.L2, "steward"),
    # --- L2 需 Steward 确认 ---
    # 导出的列名是**标签不是 API 名**（8.2）：业务方改个显示名列名就变，
    # 映射只能由人确认一次再沉淀，机器猜不了。
    "confirm_column_mapping": (Level.L2, "steward"),
    # --- L3 需 Owner 审批 ---
    "connect_source":       (Level.L3, "sponsor"),   # 起步阶段还没有 owner
    # SaaS 数据面：定时报表 → 邮件附件 → 暂存 → bronze（8.2）
    "ingest_export":        (Level.L3, "owner"),
    # SaaS 控制面：只读权限元数据。**接入必须有人担保只读**——
    # Salesforce 的 api scope 全有全无，OAuth 证明不了（8.2）
    "connect_saas_control_plane": (Level.L3, "owner"),
    "dump_saas_permissions":      (Level.L3, "owner"),
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

# ---------------------------------------------------------------------------
# 未声明工具：deny by default，不是「问一下」
#
# Hermes 自带 137 个内置工具，其中 terminal / code_execution 能执行任意命令——
# 用它们可以直接 psql 连库，绕过 Connector 的查询护栏，甚至改写 decisions 表。
#
# 把未声明工具设为 L2（发审批问人）是不够的：
#   1. 每个内置工具都发一封审批邮件 = 噪音，人会开始无脑点批准
#   2. 一旦误批一次 terminal，后果不可控——它不是单个动作，是任意动作
#
# 因此改为 L4：不在册的一律拒绝，且**不发审批打扰人**。
# 新增自己的工具时必须在 POLICY 里显式加一行——这是刻意设置的摩擦，
# 防止工具悄悄溜进可执行集合。
# ---------------------------------------------------------------------------
UNDECLARED = (Level.L4, None)

# ---------------------------------------------------------------------------
# 参数级禁令：工具名不足以描述一个动作的时候
#
# `grant_read` 是 L3（Owner 审批）——把 gold 表读权限开给某个人，正常业务。
# 但 `grant_read(principal="claw")` 是**给 Agent 自己开权限**：工具名一样，
# 动作完全不是一回事。只按工具名分级的话，L4 那组 `grant_self` /
# `modify_own_role` 就有一个绕过口——换个工具名做同一件事。
#
# 仍然是**表驱动**：加一条禁令 = 加一行，不改 hook 代码（同 POLICY）。
# 也**不发审批**——自我提权不是「问一下人就行」的事，问了反而给了它
# 一个被误批的机会。
# ---------------------------------------------------------------------------
# Agent 自己的身份。Trino / Postgres 里 Claw 用的就是这些名字。
SELF_PRINCIPALS = {"claw", "agent", "steward", "datasteward", "data_steward"}

# tool_name -> (参数名, 禁止的取值集合, 理由)
ARG_DENY: dict[str, tuple[str, set[str], str]] = {
    "grant_read": ("principal", SELF_PRINCIPALS,
                   "Agent 不得给自己开权限（readme 11.6）——"
                   "换个工具名做 grant_self 的事，同样不行"),
}


def arg_denied(tool_name: str, args: dict) -> str | None:
    """返回拒绝理由；没有禁令就返回 None。"""
    rule = ARG_DENY.get(tool_name)
    if not rule:
        return None
    key, forbidden, why = rule
    v = str((args or {}).get(key) or "").strip().lower()
    return why if v in forbidden else None


# 明确点名的高危内置工具，仅为让拒绝消息更具体
HERMES_DANGEROUS = {
    "terminal", "execute_code", "code_execution", "run_shell", "bash",
    "browser", "browser_use", "delegate_task", "write_file", "edit_file",
}


def lookup(tool_name: str) -> tuple[Level, str | None]:
    return POLICY.get(tool_name, UNDECLARED)


def is_declared(tool_name: str) -> bool:
    return tool_name in POLICY
