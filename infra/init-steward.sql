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
