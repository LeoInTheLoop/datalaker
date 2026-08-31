#!/bin/sh
# 权限隔离断言（readme 9.4 机制三）：Agent 在数据库层面无法伪造批准。
# SQLite 无列级权限，此断言只能在 Postgres 上成立 —— 这是 R0 欠账 #4。
set -u
C=datalaker-source_pg-1
pass=0; fail=0

agent()    { docker exec -e PGPASSWORD=agent_pass    -i "$C" psql -U agent_role    -d steward -h 127.0.0.1 -tAc "$1" 2>&1 | head -1; }
approver() { docker exec -e PGPASSWORD=approver_pass -i "$C" psql -U approver_role -d steward -h 127.0.0.1 -tAc "$1" 2>&1 | head -1; }

check() {  # name output expect(ok|denied)
  case "$3:$2" in
    denied:*"permission denied"*) pass=$((pass+1)); echo "  PASS  $1";;
    denied:*)                     fail=$((fail+1)); echo "  FAIL  $1  [$2]";;
    ok:*ERROR*)                   fail=$((fail+1)); echo "  FAIL  $1  [$2]";;
    ok:*)                         pass=$((pass+1)); echo "  PASS  $1";;
  esac
}

AID=$(docker exec -i "$C" psql -U postgres -d steward -tAc "SELECT gen_random_uuid()")
JTI="jti-$(date +%s)-$$"   # 每次运行唯一，保持脚本幂等

echo "=== 权限隔离：Agent vs 审批服务 ==="

# Agent 允许的写
check "Agent 可发起审批请求" "$(agent "INSERT INTO approvals (id,run_id,action_hash,tool_name,args_json,approver,expires_at) VALUES ('$AID','run-1','h1','ingest_table','{}','owner',now()+interval '72 hours')")" ok
check "Agent 可消费票据(used_at)" "$(agent "UPDATE approvals SET used_at=now() WHERE id='$AID'")" ok
check "Agent 可读 approvals"      "$(agent "SELECT count(*) FROM approvals")" ok
check "Agent 可读 decisions"      "$(agent "SELECT count(*) FROM decisions")" ok

# Agent 禁止的写 —— 核心断言
check "Agent 不能伪造批准(INSERT decisions)" "$(agent "INSERT INTO decisions (id,approval_id,decision,approver,token_jti) VALUES (gen_random_uuid(),'$AID','approve','attacker','jti-hack-'||gen_random_uuid())")" denied
check "Agent 不能改批准人"        "$(agent "UPDATE approvals SET approver='attacker' WHERE id='$AID'")" denied
check "Agent 不能延长有效期"      "$(agent "UPDATE approvals SET expires_at=now()+interval '999 days' WHERE id='$AID'")" denied
check "Agent 不能改动作指纹"      "$(agent "UPDATE approvals SET action_hash='other' WHERE id='$AID'")" denied
check "Agent 不能删除记录"        "$(agent "DELETE FROM approvals WHERE id='$AID'")" denied
check "Agent 不能改决定"          "$(agent "UPDATE decisions SET decision='approve'")" denied

# 审批服务可以
check "审批服务可写决定" "$(approver "INSERT INTO decisions (id,approval_id,decision,approver,token_jti) VALUES (gen_random_uuid(),'$AID','approve','owner@corp.com','$JTI')")" ok

# 令牌重放被唯一约束挡住
OUT=$(approver "INSERT INTO decisions (id,approval_id,decision,approver,token_jti) VALUES (gen_random_uuid(),'$AID','approve','owner@corp.com','$JTI')")
case "$OUT" in *"duplicate key"*|*"unique"*) pass=$((pass+1)); echo "  PASS  令牌重放被唯一约束拒绝";;
  *) fail=$((fail+1)); echo "  FAIL  令牌重放被唯一约束拒绝  [$OUT]";; esac

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
