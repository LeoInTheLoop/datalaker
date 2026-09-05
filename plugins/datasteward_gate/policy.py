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
    # --- L0 Hermes 自己的工具发现机制 ---
    # 非核心工具走**延迟下发**：模型先 `tool_search` 找、`tool_describe`
    # 看签名，再 `tool_call` 调。这三步里只有最后一步会真的执行动作，
    # 而 `pre_tool_call` 对 `tool_call` 触发时拿到的已经是**内层**工具名
    # （所以 `tool_call` 本身不用声明）。
    #
    # 前两步不碰任何数据 —— 但不声明就会被「未声明 = L4 拒绝」挡住，
    # 于是模型**找不到任何工具**，看起来像它什么都不会干。
    # **桩模式永远发现不了这个**：剧本直接给 `tool_call`，跳过了发现这一段。
    # 是真模型跑第一次就撞出来的。
    "tool_search":          (Level.L0, None),
    "tool_describe":        (Level.L0, None),
    # `tool_call` 是延迟下发的调用外壳。**两层都会触发 pre_tool_call**：
    # 外层拿到 "tool_call"，内层拿到真工具名（实测 connect_source 照样被
    # 挂起）。放行外层不是开后门 —— 动作本身在内层被拦。
    "tool_call":            (Level.L0, None),
    # skill 是引导层的知识文档，加载它不碰任何数据。
    "skill_view":           (Level.L0, None),
    "skills_list":          (Level.L0, None),
    # 下面这些是 config 的 `enabled_toolsets` 里显式打开的 Hermes 自带工具。
    # **打开了却不声明 = 打开了个寂寞**：模型一调就撞「未声明 = L4 拒绝」，
    # 而那条拒绝消息还在教它去改 policy.py。实测就撞在 memory 上。
    # 它们都只碰 Agent 自己的东西（记忆、待办、追问渲染），不碰源数据。
    "memory":               (Level.L0, None),
    "todo":                 (Level.L0, None),
    "clarify":              (Level.L0, None),
    # --- L0 发现 ---
    "get_table_metadata":   (Level.L0, None),
    "search_asset":         (Level.L0, None),
    "get_lineage":          (Level.L0, None),
    # 结构 + 外键 + 怎么 join。只读元数据，不碰数据内容
    "describe_asset":       (Level.L0, None),
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
    # 阶段提案：**只发问，不改东西** —— 与 propose_cleaning 同构。
    # 设成 L2 的话，「发问」这件事自己也要先被批一次，套娃。
    # 真正的门是下面那条轮级规则：没批 start_silver 就洗不了。
    "propose_stage_decision": (Level.L1, None),
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

# ---------------------------------------------------------------------------
# 什么算「同一个动作」：参与指纹的字段
#
# 默认是**全部参数**（最严）。但有些工具的参数里混着修饰性的东西 ——
# `connect_source` 的 `description` / `given_by` 是记录来源，不改变
# 「接入 acme 这个源」这件事本身。
#
# 为什么必须区分：恢复发生在新会话里，模型只知道「推进 acme 这条线」，
# 不会把当初那句 description 一字不差地再写一遍。指纹因此对不上原票据，
# 于是**又发一份新审批、又开一条新线** —— 人批过的那次白批了。
# 实测踩过两次：先是 dsn，修完之后又栽在 description 上。
#
# 表驱动：**不声明就是全字段**，只有确实混了修饰字段的工具才列进来。
# 列的是「身份字段」而不是「要剔除的字段」—— 前者漏一个会让指纹变严
# （多发一份审批，吵但安全），后者漏一个会让指纹变松（两个不同的动作
# 共用一张票，那是安全缺口）。**宁可吵，不可松。**
# ---------------------------------------------------------------------------
IDENTITY_KEYS: dict[str, tuple] = {
    "connect_source": ("source_id",),
    # 口径的**对象**是身份，口径的**内容**不是 —— 但内容绝不能不管：
    # 「把 region 改成 X」和「改成 Y」若共用一张票，就是一张票干两件事。
    # 解法不是把 value 塞进指纹，而是恢复时由门禁**回填人批准的那份参数**
    # （见 `_replay_approved_args`）：执行的永远是人看过的那一份。
    "define_semantics": ("asset", "key"),
    # 清洗的身份是**洗哪张表**；`approved_rules` / `pk` / `silver_table`
    # 是执行细节，模型每次给的都不一样。回填机制在这里价值最大：
    # **执行的清洗规则一定是人批准的那一组**，模型在恢复这一步加不了新规则。
    "apply_cleaning_rule": ("source", "table"),
    # 下面几个的参数目前很简单（恢复时刚好能给一致），但**身份是什么**
    # 现在就该写死 —— 等哪天有人给它们加个可选参数，指纹就又对不上了，
    # 而那种失败长得像「模型不听话」，查起来极贵。
    "ingest_table": ("source", "table"),
    "publish_gold": ("source", "table"),
    "grant_read": ("principal", "asset"),
    # 下面这几个还没实现（见 tests/test_toolset_whitelist.py 的
    # NOT_YET_IMPLEMENTED），但**身份属于设计，不是实现细节** ——
    # 现在写清楚，等补实现时就不会再踩一次「恢复对不上票」。
    "ingest_export": ("source", "table"),
    "full_refresh": ("source", "table"),
    "confirm_column_mapping": ("asset",),
    "connect_saas_control_plane": ("source_id",),
    "dump_saas_permissions": ("source_id",),
}


# ---------------------------------------------------------------------------
# 阶段提案的三个选项（acme_full_v2.md §3）
#
# **一处定义，三处读**：工具用它建提案、令牌用它校验点击的意图、
# 门禁用 start_silver 判断轮开没开。分成三份必然漂移 ——
# 漂移的表现是「人点了链接但决定落不进去」，又一次静默。
# 放在 policy.py 是因为这里本来就是「流程配置」那一层。
# ---------------------------------------------------------------------------
STAGE_OPTIONS = [
    {"key": "push_unfinished", "label": "先把没批下来的追完，清洗下周再说"},
    {"key": "start_silver", "label": "接得差不多了，开始清洗轮（silver）"},
    {"key": "abandon_rest", "label": "剩下那几条别等了，记为已知阻塞项"},
]
STAGE_KEYS = {o["key"] for o in STAGE_OPTIONS}

# 开清洗轮的那个 key。门禁认它，别处改名字这里必须跟着改。
STAGE_OPEN_SILVER = "start_silver"


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
