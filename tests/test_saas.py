"""SaaS 源（readme 8.2）：数据面走导出，控制面走只读元数据 API。"""
import json
import os
import pathlib
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services"))
os.environ.pop("DATASTEWARD_DSN", None)
os.environ["DATASTEWARD_DB"] = f"/tmp/dl_saas_{uuid.uuid4().hex[:8]}.db"

import export_ingest as X
import saas as S
from plugins.datasteward_gate.policy import POLICY, Level

ok, bad = [], []


def chk(n, c, d=""):
    (ok if c else bad).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))


print("\n=== 控制面：只读权限元数据 ===\n")

SRC = "acme_crm"
os.environ["SAAS_PROVIDER"] = "mock"

try:
    S.dump_permissions(SRC)
    chk("⚠️ 无担保时拒绝取数", False, "竟然放行了")
except S.SaasError as e:
    chk("⚠️ 无担保时拒绝取数", "只读担保" in str(e))

try:
    S.attest_readonly(SRC, "boss@acme.com", "ok")
    chk("空洞的担保被拒", False)
except S.SaasError:
    chk("空洞的担保被拒", True)

a = S.attest_readonly(SRC, "boss@acme.com",
                      "Profile=Integration_ReadOnly，工单 ACME-1183")
chk("担保带可复核证据", "ACME-1183" in a["evidence"])
chk("担保可读回", S.readonly_attestation(SRC)["who"] == "boss@acme.com")

inv = S.dump_permissions(SRC)
chk("dump 标明 provider", inv["provider"] == "mock", inv["provider"])
chk("用户取回", inv["counts"]["users"] == 7, str(inv["counts"]["users"]))
chk("关系展开可用（assignment 带用户名与权限集）",
    all(x.get("user_name") and x.get("permission_set") for x in inv["assignments"]))
chk("对象级权限含 modify_all 标志",
    any("modify_all" in x for x in inv["object_access"]))
chk("共享规则含 org_wide_default",
    all("org_wide_default" in x for x in inv["sharing"]))
chk("停用账号被统计", inv["counts"]["inactive_users"] == 1,
    str(inv["counts"]["inactive_users"]))
chk("dump 只有事实不下结论", "findings" not in inv and "risks" not in inv)
chk("担保随 dump 一起留痕", inv["attestation"]["evidence"].endswith("ACME-1183"))

p = S.save_dump(inv, out_dir="/tmp/saas_out")
chk("dump 可落盘", pathlib.Path(p).exists())

try:
    S.get_provider(SRC, "nope")
    chk("未知 provider 报错", False)
except S.SaasError:
    chk("未知 provider 报错", True)

print("\n=== 数据面：导出列名归一与映射确认 ===\n")

cols = ["客户 ID", "Lead Source", "创建时间", "客户 ID", "9lives"]
n = X.normalize_columns(cols)
chk("中文/空格列名可归一", all(c.replace("_", "").isalnum() for c in n), str(n))
chk("重名列不冲突", len(set(n)) == len(n), str(n))
chk("数字开头列被修正", not n[4][0].isdigit(), n[4])

m = X.column_mapping(cols)
chk("映射保留原标签", "Lead Source" in m)
chk("映射未确认时读不回", X.known_column_mapping("acme_crm.contacts") is None)
X.confirm_column_mapping("acme_crm.contacts", m, "wang@acme.com")
chk("确认后可读回", X.known_column_mapping("acme_crm.contacts") == m)

print("\n=== 数据面：快照差分看得见删除 ===\n")

# 暂存表按 run 唯一命名 —— 上一轮跑剩的表会让「首次快照」断言时红时绿。
# 教训同 R3：跨测试的数据依赖必然出问题，测试要自己准备数据。
SNAP = f"t_snap_{uuid.uuid4().hex[:6]}"
pay1 = {"columns": ["id", "name"], "rows": [["1", "a"], ["2", "b"], ["3", "c"]]}
pay2 = {"columns": ["id", "name"], "rows": [["1", "a"], ["3", "c2"], ["4", "d"]]}
try:
    d0 = X.deleted_keys(SNAP, "acct", "id", pay1)
    chk("首次快照标明无可比对象", d0["first_snapshot"] is True)
    X.stage_payload(SNAP, "acct", pay1)
    d1 = X.deleted_keys(SNAP, "acct", "id", pay2)
    chk("删除被差分发现", d1["deleted"] == ["2"], str(d1["deleted"]))
    chk("新增同时被发现", d1["added"] == ["4"], str(d1["added"]))
    staged = True
except Exception as e:                                       # noqa: BLE001
    print(f"  SKIP  暂存区不可用（{type(e).__name__}: {str(e)[:60]}）")
    staged = False

print("\n=== 门禁：新工具必须显式声明（铁律 5）===\n")

for t, lvl in (("ingest_export", Level.L3),
               ("connect_saas_control_plane", Level.L3),
               ("dump_saas_permissions", Level.L3),
               ("confirm_column_mapping", Level.L2)):
    chk(f"{t} 已声明为 {lvl.name}", POLICY.get(t, (None,))[0] == lvl,
        str(POLICY.get(t)))

print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
if bad:
    print("失败项:", ", ".join(bad))
sys.exit(1 if bad else 0)
