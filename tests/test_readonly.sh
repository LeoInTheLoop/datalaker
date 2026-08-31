#!/bin/sh
# 凭证层断言（readme 15.3 安全断言组）：只读账号必须写不了。
# 前置：docker compose --profile core up -d source_pg
set -u
C=datalaker-source_pg-1
P=${RO_PASSWORD:-ro_pass}
pass=0; fail=0

run() { docker exec -e PGPASSWORD="$P" -i "$C" psql -U agent_ro -d olist -h 127.0.0.1 -tAc "$1" 2>&1 | head -1; }

check() {  # name, output, expect_denied
  if [ "$3" = "denied" ]; then
    case "$2" in *"permission denied"*|*"must be owner"*) pass=$((pass+1)); echo "  PASS  $1";;
      *) fail=$((fail+1)); echo "  FAIL  $1  [$2]";; esac
  else
    case "$2" in *ERROR*) fail=$((fail+1)); echo "  FAIL  $1  [$2]";;
      *) pass=$((pass+1)); echo "  PASS  $1  [$2]";; esac
  fi
}

echo "=== 凭证层：只读账号 ==="
check "SELECT 允许"  "$(run 'SELECT count(*) FROM orders')"        ok
check "INSERT 拒绝"  "$(run "INSERT INTO orders VALUES (99,'x','y')")" denied
check "UPDATE 拒绝"  "$(run "UPDATE orders SET status='z'")"          denied
check "DELETE 拒绝"  "$(run 'DELETE FROM orders')"                    denied
check "DROP 拒绝"    "$(run 'DROP TABLE orders')"                     denied
check "CREATE 拒绝"  "$(run 'CREATE TABLE hack(i int)')"              denied

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
