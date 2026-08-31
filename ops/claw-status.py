#!/usr/bin/env python3
"""Claw 运维面板 —— 一屏看完。

**独立于被监控对象**：只用 stdlib + docker exec psql，
不 import 项目代码、不依赖 venv。Agent 挂掉时这个脚本照样能跑。

    python3 ops/claw-status.py           # 一次性
    python3 ops/claw-status.py --watch   # 每 30 秒刷新
"""
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PG = "datalaker-source_pg-1"

G, Y, R, D, B = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m"
X = "\033[0m"


def env():
    d = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


E = env()


def q(sql, db="steward"):
    try:
        r = subprocess.run(
            ["docker", "exec", PG, "psql", "-U", "postgres", "-d", db, "-tAF", "|", "-c", sql],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        return [l for l in r.stdout.strip().split("\n") if l]
    except Exception:
        return None


def containers():
    try:
        cmd = ["docker", "compose", "-f", str(ROOT / "infra/docker-compose.yml")]
        # compose 文件里有 ${VAR:?} 必需变量，不传 --env-file 会直接报错
        if (ROOT / ".env").exists():
            cmd += ["--env-file", str(ROOT / ".env")]
        cmd += ["ps", "--format", "{{.Name}}\t{{.Status}}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return [l.split("\t") for l in r.stdout.strip().split("\n") if "\t" in l]
    except Exception:
        return []


def bar(used, limit, width=22):
    if not limit:
        return ""
    pct = min(used / limit, 1.0)
    n = int(pct * width)
    c = R if pct >= 0.9 else (Y if pct >= 0.7 else G)
    return f"{c}{'█' * n}{D}{'░' * (width - n)}{X} {pct * 100:.0f}%"


alerts = []
print(f"\n{B}Data Steward Claw · 运维面板{X}   {D}{time.strftime('%Y-%m-%d %H:%M:%S')}{X}\n")

# ---------- 1. 服务存活 ----------
print(f"{B}服务{X}")
cs = containers()
if not cs:
    print(f"  {R}✗ 无法连接 Docker{X}")
    alerts.append("Docker 不可达")
else:
    for name, st in cs:
        up = st.startswith("Up")
        healthy = "unhealthy" not in st
        mark = f"{G}●{X}" if up and healthy else f"{R}●{X}"
        short = name.replace("datalaker-", "").rstrip("-1")
        print(f"  {mark} {short:<20} {D}{st}{X}")
        if not up:
            alerts.append(f"{short} 未运行")
        elif not healthy:
            alerts.append(f"{short} unhealthy")

# ---------- 2. 成本 ----------
print(f"\n{B}成本（今日）{X}")
row = q("SELECT count(*), coalesce(sum(prompt_tokens+output_tokens),0), "
        "coalesce(sum(cost_usd),0) FROM usage_ledger WHERE ts >= date_trunc('day', now())")
if row:
    calls, toks, cost = row[0].split("|")
    calls, toks, cost = int(calls), int(toks), float(cost)
    tl = int(E.get("DAILY_TOKEN_LIMIT", "0") or 0)
    cl = float(E.get("DAILY_COST_LIMIT_USD", "0") or 0)
    print(f"  LLM 调用   {calls:>10,}")
    print(f"  Token      {toks:>10,}   {bar(toks, tl)}")
    print(f"  花费       {cost:>10.4f} USD   {bar(cost, cl)}")
    if tl and toks >= tl:
        alerts.append(f"今日 token 已超上限 {tl:,}")
    if cl and cost >= cl:
        alerts.append(f"今日花费已超上限 ${cl}")
else:
    print(f"  {D}（usage_ledger 不可读）{X}")

# ---------- 3. 对源系统的负担 ----------
print(f"\n{B}源系统负担（今日）{X}")
row = q("SELECT count(*), count(*) FILTER (WHERE status <> 'OK'), "
        "coalesce(sum(rows_out),0), coalesce(max(duration_ms),0) "
        "FROM query_ledger WHERE ts >= date_trunc('day', now())")
if row:
    n, rej, rows, slowest = row[0].split("|")
    print(f"  查询次数   {int(n):>10,}     被拒 {int(rej)}")
    print(f"  返回行数   {int(rows):>10,}")
    print(f"  最慢查询   {float(slowest):>10.1f} ms")
    top = q("SELECT status, count(*) FROM query_ledger "
            "WHERE ts >= date_trunc('day', now()) AND status <> 'OK' "
            "GROUP BY status ORDER BY 2 DESC LIMIT 3")
    for t in (top or []):
        st, c = t.rsplit("|", 1)
        print(f"    {D}{c}× {st[:58]}{X}")
else:
    print(f"  {D}（query_ledger 不可读）{X}")

# ---------- 4. 审批进度 ----------
print(f"\n{B}审批{X}")
row = q("""SELECT
  count(*) FILTER (WHERE d.id IS NULL AND a.expires_at > now()),
  count(*) FILTER (WHERE d.id IS NULL AND a.expires_at <= now()),
  count(*) FILTER (WHERE d.decision = 'approve'),
  count(*) FILTER (WHERE d.decision = 'deny')
FROM approvals a LEFT JOIN decisions d ON d.approval_id = a.id""")
if row:
    pending, expired, ok, no = [int(x) for x in row[0].split("|")]
    wl = int(E.get("GLOBAL_WIP_LIMIT", "20") or 20)
    print(f"  待批准     {pending:>10}   {bar(pending, wl)}")
    print(f"  已批准     {ok:>10}     已拒绝 {no}     已过期 {expired}")
    if pending >= wl:
        alerts.append(f"全局在办已达上限 {wl}")
    if expired:
        alerts.append(f"{expired} 个审批已过期无人处理")
    slow = q("""SELECT a.approver, a.tool_name,
                  round(extract(epoch from (now()-a.created_at))/3600)::int
               FROM approvals a LEFT JOIN decisions d ON d.approval_id=a.id
               WHERE d.id IS NULL AND a.expires_at > now()
               ORDER BY a.created_at LIMIT 3""")
    for s_ in (slow or []):
        who, tool, hrs = s_.split("|")
        flag = R if int(hrs) >= 72 else (Y if int(hrs) >= 24 else D)
        print(f"    {flag}等待 {hrs:>3}h  {who} · {tool}{X}")
else:
    print(f"  {D}（approvals 不可读）{X}")

# ---------- 5. 告警 ----------
print()
if alerts:
    print(f"{R}{B}告警{X}")
    for a in alerts:
        print(f"  {R}▲{X} {a}")
else:
    print(f"{G}✓ 无告警{X}")
print()
sys.exit(2 if alerts else 0)
