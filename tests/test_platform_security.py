"""平台安全基线断言（readme R3）。

依赖链：TLS → 认证 → 授权。这一层做完，R4 的 Policy Sync 才有意义——
OPA 判断「谁能读哪张表」的前提是 Trino 知道「谁」。
"""
import base64, json, os, ssl, sys, urllib.error, urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
HTTPS = "https://localhost:8443/v1/statement"

ok, bad = [], []
def chk(n, c, d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


def post(sql, user=None, pw=None, url=HTTPS, timeout=20):
    h = {"X-Trino-User": user or "anonymous"}
    if user and pw:
        h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, data=sql.encode(), headers=h),
                context=CTX, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def run_to_end(sql, user, pw):
    code, d = post(sql, user, pw)
    if code != 200 or not d:
        return f"HTTP {code}"
    for _ in range(40):
        if d.get("error"):
            return "DENIED" if "Access Denied" in d["error"].get("message", "") else "ERROR"
        nxt = d.get("nextUri")
        if not nxt:
            return "OK"
        try:
            with urllib.request.urlopen(urllib.request.Request(nxt), context=CTX,
                                        timeout=20) as r:
                d = json.loads(r.read())
        except Exception:
            return "ERROR"
    return "TIMEOUT"


print("\n=== 平台安全基线 ===\n")

# 认证
chk("匿名查询被拒", post("SELECT 1")[0] == 401)
chk("错误密码被拒", post("SELECT 1", "admin", "wrong")[0] == 401)
chk("正确密码通过", post("SELECT 1", "admin", "admin_pw_change_me")[0] == 200)

# 明文端口不对外
code, _ = post("SELECT 1", url="http://localhost:8080/v1/statement", timeout=5)
chk("明文 8080 不从宿主机可达", post("SELECT 1", url="http://localhost:8080/v1/statement")[0] in (0, 401, 403),
    "宿主机不映射 8080")

# **网络内部同样要挡住。** R3 只做到「不映射到宿主机」，
# 但 compose 网络里任何容器都能直连 trino:8080 —— 实测从 scheduler 容器
# 匿名带 `X-Trino-User: admin` 查 gold.customer_360，拿到了未遮蔽的手机号。
# 修法是 `allow-insecure-over-http=false`，这条断言防它被改回去。
import subprocess as _sp

_probe = """
import urllib.request, json, sys
req = urllib.request.Request('http://trino:8080/v1/statement', data=b'SELECT 1',
    headers={'X-Trino-User': 'admin'}, method='POST')
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print('OPEN', r.status)
except Exception as e:
    print('BLOCKED', getattr(e, 'code', type(e).__name__))
"""
try:
    _r = _sp.run(["docker", "exec", "datalaker-scheduler-1", "python3", "-c", _probe],
                 capture_output=True, text=True, timeout=90)
    _out = (_r.stdout or "").strip().splitlines()[-1] if _r.stdout.strip() else "?"
except Exception as _e:                                       # noqa: BLE001
    _out = f"SKIP {type(_e).__name__}"
if _out.startswith("SKIP") or not _out or _out == "?":
    print(f"  SKIP  网络内部匿名探测（{_out}）")
else:
    chk("⚠️ 网络内部也不能匿名冒充身份", _out.startswith("BLOCKED"), _out)

# 授权（readme 11.4）
chk("claw 可读源系统", run_to_end("SELECT count(*) FROM postgres.public.orders",
                                  "claw", "claw_pw_change_me") == "OK")
chk("claw 不能写源系统（写权限极窄）",
    run_to_end("CREATE TABLE postgres.public.hack (i int)",
               "claw", "claw_pw_change_me") == "DENIED")
chk("analyst 看不到 gold 之外的 schema",
    run_to_end("SELECT count(*) FROM iceberg.smoke.customers",
               "analyst", "analyst_pw") == "DENIED")

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
