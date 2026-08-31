-- 凭证层（readme 第 8 节 第一层）：Agent 专用只读账号
--
-- 铁律：Agent 进程环境中不存在任何具备写权限的源系统凭证。
-- 「不损伤原数据」由此保证，而非依赖 Agent 自觉。

DROP ROLE IF EXISTS agent_ro;
CREATE ROLE agent_ro LOGIN PASSWORD :'ro_password';

GRANT CONNECT ON DATABASE olist TO agent_ro;
GRANT USAGE  ON SCHEMA public   TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;

-- 后续新建的表也自动只读
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_ro;

-- 明确不授予：INSERT / UPDATE / DELETE / TRUNCATE / CREATE
