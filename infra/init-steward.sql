-- 治理库：两表 append-only 审批模型 + 列级权限隔离（readme 9.4 机制三）
--
-- 核心保证：Agent 在**数据库层面**无法伪造批准，不依赖任何代码正确性。
-- 断言见 tests/test_grants.sh（12/12）。

CREATE TABLE IF NOT EXISTS approvals (
  id           uuid PRIMARY KEY,
  run_id       text NOT NULL,
  action_hash  text NOT NULL,           -- sha256(tool_name || canonical_json(args))
  tool_name    text NOT NULL,
  args_json    jsonb NOT NULL,
  approver     text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,    -- 72h，与 Escalation 时间线对齐
  used_at      timestamptz              -- 消费即失效，防重放
);

CREATE TABLE IF NOT EXISTS decisions (
  id           uuid PRIMARY KEY,
  approval_id  uuid NOT NULL REFERENCES approvals(id),
  decision     text NOT NULL CHECK (decision IN ('approve','deny')),
  approver     text NOT NULL,
  decided_at   timestamptz NOT NULL DEFAULT now(),
  token_jti    text UNIQUE NOT NULL,    -- 令牌一次性：重放被唯一约束挡住
  message_id   text,                    -- 触发邮件的 Message-ID
  client_ip    inet,
  user_agent   text
);

CREATE INDEX IF NOT EXISTS ix_appr_hash ON approvals(action_hash, run_id);
CREATE INDEX IF NOT EXISTS ix_dec_appr  ON decisions(approval_id);

-- ---------------------------------------------------------------------------
-- Agent 角色：可发起请求、可消费票据，**写不了决定**
-- ---------------------------------------------------------------------------
CREATE ROLE agent_role LOGIN PASSWORD :'agent_password';
GRANT CONNECT ON DATABASE steward TO agent_role;
GRANT USAGE ON SCHEMA public TO agent_role;
GRANT INSERT (id, run_id, action_hash, tool_name, args_json, approver, created_at, expires_at)
      ON approvals TO agent_role;
GRANT UPDATE (used_at) ON approvals TO agent_role;
GRANT SELECT ON approvals, decisions TO agent_role;
-- 关键：不授予 decisions 的 INSERT / UPDATE，也不授予 approvals 其他列的 UPDATE

-- ---------------------------------------------------------------------------
-- 审批 callback 服务角色：唯一能写决定的身份。必须是独立进程（CLAUDE.md 铁律 2）
-- ---------------------------------------------------------------------------
CREATE ROLE approver_role LOGIN PASSWORD :'approver_password';
GRANT CONNECT ON DATABASE steward TO approver_role;
GRANT USAGE ON SCHEMA public TO approver_role;
GRANT SELECT ON approvals TO approver_role;
GRANT INSERT, SELECT ON decisions TO approver_role;

-- ---------------------------------------------------------------------------
-- 下面五张表原来只存在于 SQLite 的 DDL 里，Postgres 侧一张都没建。
--
-- 后果分两种，都很隐蔽：
--   · `GRANT ... ON asset_semantics` 会直接报错，整个 init 脚本半途而废
--   · 侥幸建过一次的（R3 手工建的 sync_state）能跑，没建过的静默返回空 ——
--     `usage_ledger` 就是这样：预算兜底（5.6）读到的永远是 0，看着像「没超」
--
-- **两个后端的表集合必须一致。** 加一张表要同步改两处：
-- `plugins/datasteward_gate/approvals.py` 的 DDL 与这里。有断言盯着。
-- ---------------------------------------------------------------------------

-- 角色化：绑角色不绑人（readme 10.4）。
-- 审批人解析、入站白名单（current_holders）都靠它。
CREATE TABLE IF NOT EXISTS role_assignment (
    id          BIGSERIAL PRIMARY KEY,
    role        TEXT NOT NULL,
    person      TEXT NOT NULL,
    valid_from  DOUBLE PRECISION NOT NULL,
    valid_to    DOUBLE PRECISION,
    granted_by  TEXT NOT NULL,
    reason      TEXT
);
GRANT SELECT, INSERT, UPDATE ON role_assignment TO agent_role;
GRANT USAGE, SELECT ON SEQUENCE role_assignment_id_seq TO agent_role;
GRANT SELECT ON role_assignment TO approver_role;

-- 业务知识沉淀：问过的不再问（readme 5.7）
CREATE TABLE IF NOT EXISTS asset_semantics (
    id           BIGSERIAL PRIMARY KEY,
    asset        TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at DOUBLE PRECISION NOT NULL,
    source_item  TEXT,
    UNIQUE(asset, key)
);
GRANT USAGE, SELECT ON SEQUENCE asset_semantics_id_seq TO agent_role;

-- 运维账本（readme 20.1）：落库而非进程内存，监控独立于被监控对象
CREATE TABLE IF NOT EXISTS query_ledger (
    id          BIGSERIAL PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
    source_id   TEXT NOT NULL,
    purpose     TEXT,
    sql_text    TEXT NOT NULL,
    rows_out    BIGINT NOT NULL DEFAULT 0,
    duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    est_rows    DOUBLE PRECISION,
    status      TEXT NOT NULL
);
GRANT SELECT, INSERT ON query_ledger TO agent_role;
GRANT USAGE, SELECT ON SEQUENCE query_ledger_id_seq TO agent_role;

CREATE TABLE IF NOT EXISTS usage_ledger (
    id            BIGSERIAL PRIMARY KEY,
    ts            DOUBLE PRECISION NOT NULL,
    run_id        TEXT,
    model         TEXT NOT NULL,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
    purpose       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    seq     BIGSERIAL PRIMARY KEY,
    run_id  TEXT NOT NULL,
    ts      DOUBLE PRECISION NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT
);
GRANT SELECT, INSERT ON events TO agent_role;
GRANT USAGE, SELECT ON SEQUENCE events_seq_seq TO agent_role;

-- 业务知识沉淀：Agent 可更新（口径会修订）。
-- 与 decisions 的只读约束是两回事——那张表关乎审批权威，这张不。
GRANT SELECT, INSERT, UPDATE ON asset_semantics TO agent_role;

-- 增量同步状态（readme 6.1）
CREATE TABLE IF NOT EXISTS sync_state (
    asset            TEXT PRIMARY KEY,
    strategy         TEXT NOT NULL,
    watermark        TEXT,
    last_synced_at   TIMESTAMPTZ,
    data_as_of       TIMESTAMPTZ,
    freshness_sla_h  INTEGER NOT NULL DEFAULT 24,
    schema_hash      TEXT,
    row_count        BIGINT,
    last_error       TEXT
);
GRANT SELECT, INSERT, UPDATE ON sync_state TO agent_role;

-- 任务注册表（readme 2 / 4.1）：多条长时任务并行挂起与恢复
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    params      TEXT NOT NULL,
    status      TEXT NOT NULL,
    waiting_on  TEXT,
    checkpoint  TEXT,
    note        TEXT,
    owner_role  TEXT,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    resumed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
GRANT SELECT, INSERT, UPDATE ON runs TO agent_role;

-- Remediation Ledger（readme 7）：治理动作的审计轨 + 给源系统的整改清单。
--
-- **只在 lake 里清洗，等于给源头的缺陷付永久利息。**
-- 因此这张表的价值不在建议的数量，而在被采纳的比例，以及
-- 「清洗规则数量在下降」这个反直觉的成功指标。
CREATE TABLE IF NOT EXISTS remediation_ledger (
    rl_id            TEXT PRIMARY KEY,
    source_table     TEXT NOT NULL,
    field            TEXT,
    issue_type       TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT 'data',   -- data / permission
    observed_pattern TEXT,
    action_taken     TEXT,
    confirmed_by     TEXT,
    message_id       TEXT,
    suggestion       TEXT,
    owner_role       TEXT,
    status           TEXT NOT NULL DEFAULT 'open',   -- open/proposed/accepted/fixed/rejected
    reject_reason    TEXT,
    recurrences      INTEGER NOT NULL DEFAULT 1,
    rows_cleaned     BIGINT NOT NULL DEFAULT 0,
    rule_revisions   INTEGER NOT NULL DEFAULT 0,
    first_seen       DOUBLE PRECISION NOT NULL,
    last_seen        DOUBLE PRECISION NOT NULL,
    due_at           DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_rl_status ON remediation_ledger(status);

-- 清洗规则。**每条都要标注它在补哪个洞**（readme 7）——
-- 没有 ledger_ref 的规则无法退役，因为没人知道它为什么存在。
CREATE TABLE IF NOT EXISTS cleaning_rules (
    name         TEXT PRIMARY KEY,
    ledger_ref   TEXT NOT NULL,
    fixes        TEXT NOT NULL,
    retire_when  TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    revisions    INTEGER NOT NULL DEFAULT 0,
    created_at   DOUBLE PRECISION NOT NULL,
    retired_at   DOUBLE PRECISION
);
GRANT SELECT, INSERT, UPDATE ON remediation_ledger, cleaning_rules TO agent_role;

-- 用量账本：Agent 记自己的花销。
-- **没有这一条，预算兜底（readme 5.6）读到的永远是 0** —— 以前账本里
-- 只有「扮演人的那个模型」的花销，Agent 自己一分钱都没记进去。
-- 仍然与铁律 2 无关：decisions 对 agent_role 依旧只有 SELECT。
GRANT SELECT, INSERT ON usage_ledger TO agent_role;
GRANT USAGE, SELECT ON SEQUENCE usage_ledger_id_seq TO agent_role;
