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

-- 业务知识沉淀：Agent 可更新（口径会修订）。
-- 与 decisions 的只读约束是两回事——那张表关乎审批权威，这张不。
GRANT UPDATE ON asset_semantics TO agent_role;

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
