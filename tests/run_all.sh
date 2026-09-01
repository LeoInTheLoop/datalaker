#!/bin/sh
# 全量断言。安全类要求 100% 通过（readme 15.3）。
#
#   ./tests/run_all.sh
#
# 需要 docker core 组已启动：cd infra && docker compose --profile core up -d minio source_pg
set -u
cd "$(dirname "$0")/.." || exit 1

export DATASTEWARD_DB=${DATASTEWARD_DB:-/tmp/dl_test.db}
# 存储后端必须逐段显式控制：DATASTEWARD_DSN 一旦设置，
# open_store 全局走 Postgres——包括那些只想用 SQLite 的隔离测试
PG_DSN="${DATASTEWARD_DSN:-}"
export DATASTEWARD_TOKEN_SECRET=${DATASTEWARD_TOKEN_SECRET:-test-secret}
export APPROVAL_PORT=${APPROVAL_PORT:-8787}
rc=0

echo "########## 0. L0 平台冒烟（datalake 独立可用） ##########"
if docker ps --format '{{.Names}}' | grep -q datalaker-trino-1; then
  python3 tests/test_lakehouse_smoke.py || rc=1
else
  echo "  SKIP  Trino 未启动（cd infra && docker compose --profile core up -d）"
fi

echo ""
echo "########## 0.2 平台安全基线（TLS/认证/授权） ##########"
if docker ps --format '{{.Names}}' | grep -q datalaker-trino-1; then
  ./.venv/bin/python tests/test_platform_security.py 2>/dev/null || python3 tests/test_platform_security.py || rc=1
else
  echo "  SKIP  Trino 未启动"
fi

echo ""
echo "########## 0.5 工具白名单（安全主防线） ##########"
python3 tests/test_toolset_whitelist.py || rc=1

echo ""
echo "########## 1. 治理 Plugin 拦截 ##########"
DATASTEWARD_DB=/tmp/dl_gate.db ( unset DATASTEWARD_DSN; python3 tests/test_gate.py ) || rc=1

echo ""
PY=./.venv/bin/python; [ -x "$PY" ] || PY=python3
echo "########## 1.2 数据工具 + Pipeline ##########"
if docker ps --format '{{.Names}}' | grep -q datalaker-source_pg-1; then
  $PY tests/test_data_tools.py || rc=1
  $PY tests/test_pipeline.py || rc=1
  $PY tests/test_memory.py || rc=1
  $PY tests/test_sql_admission.py || rc=1
  $PY tests/test_ingest_paths.py || rc=1
  $PY tests/test_sync.py || rc=1
else
  echo "  SKIP  Postgres 未启动"
fi

echo ""
echo "########## 1.5 R2 Harness（角色化/WIP/提问/沉淀/升级） ##########"
DATASTEWARD_DB=/tmp/dl_r2.db ( unset DATASTEWARD_DSN; python3 tests/test_r2_harness.py ) || rc=1
rm -f /tmp/dl_esc.db*
DATASTEWARD_DB=/tmp/dl_esc.db ( unset DATASTEWARD_DSN; python3 tests/test_escalation.py ) || rc=1

echo ""
echo "########## 2. 凭证层（源系统只读） ##########"
if docker ps --format '{{.Names}}' | grep -q datalaker-source_pg-1; then
  ./tests/test_readonly.sh || rc=1
  echo ""
  echo "########## 3. 权限隔离（列级 GRANT） ##########"
  ./tests/test_grants.sh || rc=1
else
  echo "  SKIP  Postgres 未启动（cd infra && docker compose --profile core up -d source_pg）"
fi

echo ""
echo "########## 4. Connector Service ##########"
if docker ps --format '{{.Names}}' | grep -q datalaker-source_pg-1; then
  PY=./.venv/bin/python; [ -x "$PY" ] || PY=python3
  $PY tests/test_connector.py || rc=1
else
  echo "  SKIP  Postgres 未启动"
fi

echo ""
echo "########## 5. Hermes 真实集成（pre_tool_call） ##########"
HERMES_DIR=${HERMES:-}
if [ -n "$HERMES_DIR" ] && [ -x "$HERMES_DIR/.venv-h/bin/python" ]; then
  # Hermes 集成测的是 hook 契约而非存储；用 SQLite 隔离，
  # 且 .venv-h 里没有 psycopg
  ( unset DATASTEWARD_DSN
    DATASTEWARD_DB=/tmp/dl_hermes.db HERMES="$HERMES_DIR" \
      "$HERMES_DIR/.venv-h/bin/python" tests/test_hermes_integration.py ) || rc=1
else
  echo "  SKIP  未设置 HERMES 环境变量（见 docs/handoff/R1.md）"
fi

echo ""
echo "########## 6. 通知失败 ≠ 门禁打开 ##########"
DATASTEWARD_DB=/tmp/dl_mail_iso.db ( unset DATASTEWARD_DSN; python3 tests/test_mail_isolation.py ) || rc=1

echo ""
echo "########## 7. 挂起语义（Hermes error 格式） ##########"
[ -f .env ] && python3 tests/test_pending_semantics.py || echo "  SKIP  .env 不存在"

echo ""
echo "########## 8. 模型端点 + tool calling ##########"
if [ -f .env ]; then
  python3 tests/test_model_endpoint.py || rc=1
else
  echo "  SKIP  .env 不存在"
fi

echo ""
echo "########## 9. 审批闭环（端到端） ##########"
rm -f "$DATASTEWARD_DB" "$DATASTEWARD_DB-wal" "$DATASTEWARD_DB-shm"
python3 services/approval_callback.py > /tmp/cb_test.log 2>&1 &
CB=$!
python3 - <<'PY'
import urllib.request, time, os
port = os.environ.get("APPROVAL_PORT", "8787")
for _ in range(50):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.5); break
    except Exception: time.sleep(.1)
PY
( unset DATASTEWARD_DSN; python3 tests/test_callback_e2e.py ) || rc=1
kill $CB 2>/dev/null

echo ""
if [ "$rc" -eq 0 ]; then echo "✅ 全部通过"; else echo "❌ 有失败项"; fi
exit $rc
