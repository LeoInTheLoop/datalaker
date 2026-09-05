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
PY=./.venv/bin/python; [ -x "$PY" ] || PY=python3

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
( unset DATASTEWARD_DSN; DATASTEWARD_DB=/tmp/dl_gate.db $PY tests/test_gate.py ) || rc=1

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
  ( unset DATASTEWARD_DSN; $PY tests/test_double_confirm.py ) || rc=1
  ( unset DATASTEWARD_DSN; $PY tests/test_inbound.py ) || rc=1
  ( unset DATASTEWARD_DSN; $PY tests/test_inbound_en.py ) || rc=1
  ( unset DATASTEWARD_DSN; $PY tests/test_scenario_discovery.py ) || rc=1
else
  echo "  SKIP  Postgres 未启动"
fi

echo ""
echo "########## 1.3 停止点判定 + DQ 门槛（5.2 / 5.3） ##########"
( unset DATASTEWARD_DSN; $PY tests/test_stop_dq.py ) || rc=1

echo ""
echo "########## 1.32 审批人解析（归属 → 前缀 → 兜底） ##########"
( unset DATASTEWARD_DSN; $PY tests/test_ownership_routing.py ) || rc=1

echo ""
echo "########## 1.33 DQ 规则库（纯函数，不连库） ##########"
python3 tests/test_dq_rules.py || rc=1

echo ""
echo "########## 1.36 治理产出：Ledger + 权限发现 ##########"
( unset DATASTEWARD_DSN; $PY tests/test_ledger.py ) || rc=1
if docker exec datalaker-source_pg-1 pg_isready -U postgres >/dev/null 2>&1; then
  ( unset DATASTEWARD_DSN; $PY tests/test_perm_discovery.py ) || rc=1
  ( unset DATASTEWARD_DSN; $PY tests/test_policy_sync.py ) || rc=1
  ( unset DATASTEWARD_DSN; $PY tests/test_clean.py ) || rc=1
else
  echo "  SKIP  Postgres 未启动"
fi

echo ""
echo "########## 1.365 发布到 gold（没分类不发布 / _raw 不出去） ##########"
# 前两组不连 Trino，建表那组自己会 SKIP
( unset DATASTEWARD_DSN; $PY tests/test_publish.py ) || rc=1

echo ""
echo "########## 1.34 时间引擎：12 天升级链路（秒级） ##########"
( unset DATASTEWARD_DSN; $PY tests/test_clock.py ) || rc=1

echo ""
echo "########## 1.35 长时任务：并行挂起与恢复 ##########"
( unset DATASTEWARD_DSN; $PY tests/test_runs.py ) || rc=1

echo ""
echo "########## 1.37 可观测：OpenTelemetry → Phoenix ##########"
# no-op 与「组件挂了照跑」不依赖任何服务；连 Phoenix 那一段测试自己会 SKIP
( unset DATASTEWARD_DSN; $PY tests/test_tracing.py ) || rc=1

echo ""
echo "########## 1.4 SaaS 源（导出数据面 / 只读控制面） ##########"
( unset DATASTEWARD_DSN; $PY tests/test_saas.py ) || rc=1

echo ""
echo "########## 1.5 R2 Harness（角色化/WIP/提问/沉淀/升级） ##########"
( unset DATASTEWARD_DSN; DATASTEWARD_DB=/tmp/dl_r2.db python3 tests/test_r2_harness.py ) || rc=1
rm -f /tmp/dl_esc.db*
( unset DATASTEWARD_DSN; DATASTEWARD_DB=/tmp/dl_esc.db python3 tests/test_escalation.py ) || rc=1

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
echo "########## 4.5 连表：源禁 join / lake 可 join（铁律 3） ##########"
# lake 连不上时它自己 SKIP 掉执行那几条，其余（准入规则）不依赖 docker
$PY tests/test_join.py || rc=1

echo ""
echo "########## 5.2 入站走 Hermes 自己的 adapter（GreenMail 收件箱） ##########"
if [ -n "$HERMES_DIR" ] && [ -x "$HERMES_DIR/.venv-h/bin/python" ]; then
  # GreenMail 没起时它自己 SKIP（探活走干活同一条 SMTP 路）
  HERMES="$HERMES_DIR" "$HERMES_DIR/.venv-h/bin/python" tests/test_hermes_inbound.py || rc=1
else
  echo "  SKIP  未设置 HERMES 环境变量"
fi

echo ""
echo "########## 5.3 入站整链：真网关 → 会话 → 工具 → 回信 ##########"
# 起真的 hermes gateway；GreenMail 没起时它自己 SKIP
if [ -n "${HERMES:-}" ]; then
  $PY tests/test_gateway_inbound.py || rc=1
else
  echo "  SKIP  未设置 HERMES"
fi

echo ""
echo "########## 5.5 Hermes 工具环路（工具真跑在 Hermes 里 + 门禁在路上） ##########"
$PY tests/test_hermes_tool_loop.py || rc=1

echo ""
echo "########## 5.55 恢复整链（挂起 → 批准 → cron 唤醒 → 写 bronze → 收线） ##########"
# 零件各测各的已经绿过，链子断掉是本项目摔过六次的形状。这组接整条。
# 不接 docker 时它自己按 lake 探活 SKIP 掉「真写 bronze」那一条。
$PY tests/test_resume_cron.py || rc=1

echo ""
echo "########## 5.6b 周报改成提案（skill → 工具 → 门禁三层串起来） ##########"
# 起自己的 callback（8791），不与第 9 组的 8787 抢端口
$PY tests/test_weekly_proposal.py || rc=1

echo ""
echo "########## 0.6 两个后端的表集合一致（纯静态） ##########"
python3 tests/test_schema_parity.py || rc=1

echo ""
echo "########## 5.7 预算兜底的数据来源（Agent 自己的花销） ##########"
( unset DATASTEWARD_DSN; $PY tests/test_usage_budget.py ) || rc=1

echo ""
echo "########## 5.6 定时任务归 Hermes 的 cron（交接面） ##########"
python3 tests/test_cron_migration.py || rc=1

echo ""
echo "########## 6. 通知失败 ≠ 门禁打开 ##########"
( unset DATASTEWARD_DSN; DATASTEWARD_DB=/tmp/dl_mail_iso.db python3 tests/test_mail_isolation.py ) || rc=1

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
NOTIFY_CHANNEL=outbox NOTIFY_OUTBOX=/tmp/cb_outbox.jsonl \
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
echo "########## 9.5 大 case：5 源 · 10 人 · 多线并行 · 跨 12 天 ##########"
# 探活走和干活同一条路（带凭证）——否则加了认证之后会静默跳过整组
if $PY -c "import sys;sys.path[:0]=['services','plugins'];import sync;sync._trino('SELECT 1')" >/dev/null 2>&1; then
  ( unset DATASTEWARD_DSN; $PY tests/run_case_full.py > /tmp/full_case.log 2>&1 ) \
    && sed -n '/=== 判定 ===/,$p' /tmp/full_case.log \
    || { sed -n '/=== 判定 ===/,$p' /tmp/full_case.log; tail -20 /tmp/full_case.log; rc=1; }
else
  echo "  SKIP  Trino 未启动"
fi

echo ""
echo "########## 9.6 P2 地基：邮件模拟环境 + case v2 结构 ##########"
# mailsim 需要 mail 组（cd infra && docker compose --profile mail up -d greenmail）
# 没起时它自己 SKIP —— 探活走 mailsim.probe()，与干活同一条 SMTP 路
$PY tests/test_mailsim.py || rc=1
( unset DATASTEWARD_DSN; $PY tests/test_case_v2.py ) || rc=1
( unset DATASTEWARD_DSN; $PY tests/test_score_v2.py ) || rc=1

echo ""
echo "########## 9.7 v2 大 case：滚雪球发现 · 越权陷阱 · 停止信号 · 阶段提案 ##########"
# 主语是 Hermes：工具由它分发、门禁在那条路上，脚本只扮人。
# 前置任缺一样它自己退出 2 并点名 —— 那时算失败，不是跳过。
if $PY -c "import sys;sys.path[:0]=['services','plugins'];import sync;sync._trino('SELECT 1')" >/dev/null 2>&1    && [ -n "${HERMES:-}" ]; then
  V2DB=/tmp/dl_v2case.db
  rm -f "$V2DB" "$V2DB-wal" "$V2DB-shm" "$V2DB.outbox.jsonl"
  ( unset DATASTEWARD_DSN
    export DATASTEWARD_DB=$V2DB NOTIFY_CHANNEL=outbox NOTIFY_OUTBOX=$V2DB.outbox.jsonl
    export APPROVAL_PORT=8792 APPROVAL_BASE_URL=http://127.0.0.1:8792 REQUIRE_DOUBLE_CONFIRM=0
    export EVAL_SOURCE_DSN=${EVAL_SOURCE_DSN:-postgresql://agent_ro:ro_pass@127.0.0.1:5432/acme}
    $PY -c "
import sys,json,os; sys.path[:0]=['evals','services']
import snapshot
m={'schema':'public','tables':['fin_invoice','fin_monthly','legacy_export']}
json.dump(snapshot.take(m, dsn=os.environ['EVAL_SOURCE_DSN']),
          open('/tmp/dl_v2_snap.json','w'), ensure_ascii=False)" 2>/dev/null
    export CLAW_SNAP_BEFORE=/tmp/dl_v2_snap.json
    $PY services/approval_callback.py > /tmp/dl_v2_cb.log 2>&1 &
    CB2=$!
    sleep 2
    $PY tests/run_case_v2.py --out /tmp/dl_v2_report > /tmp/dl_v2_case.log 2>&1
    RC=$?
    kill $CB2 2>/dev/null
    tail -3 /tmp/dl_v2_case.log
    exit $RC ) || rc=1
else
  echo "  SKIP  Trino 未启动或未设置 HERMES"
fi

echo ""
echo "########## 10. Eval 体外隔离（判分器不许 import 被测代码） ##########"
python3 evals/test_isolation.py || rc=1

echo ""
if [ "$rc" -eq 0 ]; then echo "✅ 全部通过"; else echo "❌ 有失败项"; fi
exit $rc
