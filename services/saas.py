"""SaaS 控制面：权限元数据只读 dump（readme 8.2 / 11.2）。

这是导出唯一覆盖不了、又不能不要的部分：
**谁带 `Modify All Data`、离职者账号是否仍 Active、共享规则开到什么程度**
—— 报表导不出来，只能读 Profile / PermissionSet / SharingRules。

它不是数据管道，是一次性的元数据读取，因此约束与数据面完全不同：

| | 数据面（导出） | 控制面（这里） |
|---|---|---|
| 频率 | 每天 | 接入时一次 + 每周复查 |
| 量级 | 万级行 | 几十次调用 |
| **关系展开** | 不适用 | **允许** |
| 配额 | 不消耗 | 可忽略 |

**关系展开在这里是允许的**，因为 `SELECT PermissionSet.Name, Assignee.Name
FROM PermissionSetAssignment` 是唯一合理写法，禁掉只会逼出更贵的 N+1。
这不是为 SaaS 新造的例外 —— Connector 的 `_meta_exec` 在 `pg_catalog` 上
本来就允许 join，同一条分界画在 setup 对象上而已。

## 只读证明不了，所以必须有人担保

Salesforce 的 `api` scope 全有全无：拿到 token 就能写。
**OAuth 层面证明不了只读**，因此接入前必须让 Owner 确认「这个连接用户是只读的」，
并把证据存进 Ledger（`attest_readonly`）。没有担保就不给连 —— 这一条由
`policy.py` 的 `connect_saas_control_plane`（L3）在门禁层强制，不靠本模块自觉。

本模块产出**只有事实，没有结论**。「谁权限过大」是 R4 的判断（11.2），
取数与出结论分开，才能让结论被复核。
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ATTEST_KEY = "saas_readonly_attestation"


class SaasError(RuntimeError):
    pass


# ---------------------------------------------------------------- 只读担保
def attest_readonly(source_id: str, who: str, evidence: str,
                    approval_id: str | None = None) -> dict:
    """记录「连接用户是只读的」这一担保。

    `evidence` 应当是可复核的东西 —— Profile 名、权限截图链接、工单号，
    而不是一句「我确认」。存进记忆层，R4 出结论时一并引用。
    """
    if not evidence or len(evidence.strip()) < 4:
        raise SaasError("担保必须附可复核的证据（Profile 名 / 截图链接 / 工单号）")
    from plugins.datasteward_gate.approvals import open_store
    rec = {"who": who, "evidence": evidence.strip(), "approval_id": approval_id}
    with open_store(readonly=False, init_schema=True) as st:
        st.remember(source_id, ATTEST_KEY, json.dumps(rec, ensure_ascii=False), who)
    return {"source_id": source_id, **rec, "attested": True}


def readonly_attestation(source_id: str) -> dict | None:
    from plugins.datasteward_gate.approvals import open_store
    try:
        with open_store(readonly=True, init_schema=False) as st:
            v = st.known(source_id, ATTEST_KEY)
    except Exception:                                        # noqa: BLE001
        return None
    if not v:
        return None
    try:
        return json.loads(v["value"] if isinstance(v, dict) else v[0])
    except Exception:                                        # noqa: BLE001
        return None


# ---------------------------------------------------------------- provider
class Provider:
    """所有 SaaS 控制面实现同一组方法，产出同一种归一结构。

    归一是重点：R4 的权限发现不该为每个 SaaS 写一遍判断逻辑。
    """

    name = "base"

    def users(self) -> list:
        """[{id, name, email, active, profile, last_login}]"""
        raise NotImplementedError

    def permission_sets(self) -> list:
        """[{id, name, label, permissions: [...]}]"""
        raise NotImplementedError

    def assignments(self) -> list:
        """[{user_id, permission_set, via}] —— 这一步就是「关系展开」。"""
        raise NotImplementedError

    def object_access(self) -> list:
        """[{profile_or_set, object, read, create, edit, delete, view_all, modify_all}]"""
        raise NotImplementedError

    def sharing(self) -> list:
        """[{object, org_wide_default, rules: [...]}]"""
        return []


class MockProvider(Provider):
    """从 JSON fixture 读。用于测试与演练 —— 没有真 org 时它是唯一诚实的选择。

    fixture 路径：`SAAS_FIXTURE` 或 `data/saas/<source_id>.json`。
    **它标明自己是 mock**，dump 结果里带 `provider="mock"`，
    不会被误当成真实取数。
    """

    name = "mock"

    def __init__(self, source_id):
        p = pathlib.Path(os.environ.get("SAAS_FIXTURE")
                         or ROOT / "data" / "saas" / f"{source_id}.json")
        if not p.exists():
            raise SaasError(f"没有 fixture：{p}")
        self.d = json.loads(p.read_text(encoding="utf-8"))

    def users(self):
        return self.d.get("users", [])

    def permission_sets(self):
        return self.d.get("permission_sets", [])

    def assignments(self):
        return self.d.get("assignments", [])

    def object_access(self):
        return self.d.get("object_access", [])

    def sharing(self):
        return self.d.get("sharing", [])


class SalesforceProvider(Provider):
    """真 org。SOQL over REST，**只发 SELECT**。

    每条查询都展开关系（`PermissionSet.Name`、`Assignee.Name`）——
    这正是控制面允许而数据面禁止的那件事。
    """

    name = "salesforce"
    QUERIES = {
        "users": "SELECT Id, Name, Email, IsActive, Profile.Name, LastLoginDate FROM User",
        "permission_sets": "SELECT Id, Name, Label FROM PermissionSet",
        "assignments": ("SELECT Assignee.Id, Assignee.Name, PermissionSet.Name "
                        "FROM PermissionSetAssignment"),
        "object_access": ("SELECT Parent.Profile.Name, SobjectType, PermissionsRead, "
                          "PermissionsCreate, PermissionsEdit, PermissionsDelete, "
                          "PermissionsViewAllRecords, PermissionsModifyAllRecords "
                          "FROM ObjectPermissions"),
    }

    def __init__(self, source_id):
        self.base = os.environ.get("SF_INSTANCE_URL", "")
        self.token = os.environ.get("SF_ACCESS_TOKEN", "")
        if not (self.base and self.token):
            raise SaasError("缺 SF_INSTANCE_URL / SF_ACCESS_TOKEN —— "
                            "没有真 org 时请用 provider='mock'，不要伪造数据")

    def _soql(self, q):
        import urllib.parse
        import urllib.request
        url = (self.base.rstrip("/") + "/services/data/v60.0/query?q="
               + urllib.parse.quote(q))
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("records", [])

    def users(self):
        return [{"id": r["Id"], "name": r.get("Name"), "email": r.get("Email"),
                 "active": r.get("IsActive"),
                 "profile": (r.get("Profile") or {}).get("Name"),
                 "last_login": r.get("LastLoginDate")}
                for r in self._soql(self.QUERIES["users"])]

    def permission_sets(self):
        return [{"id": r["Id"], "name": r.get("Name"), "label": r.get("Label")}
                for r in self._soql(self.QUERIES["permission_sets"])]

    def assignments(self):
        return [{"user_id": (r.get("Assignee") or {}).get("Id"),
                 "user_name": (r.get("Assignee") or {}).get("Name"),
                 "permission_set": (r.get("PermissionSet") or {}).get("Name"),
                 "via": "permission_set"}
                for r in self._soql(self.QUERIES["assignments"])]

    def object_access(self):
        out = []
        for r in self._soql(self.QUERIES["object_access"]):
            prof = ((r.get("Parent") or {}).get("Profile") or {}).get("Name")
            out.append({"profile_or_set": prof, "object": r.get("SobjectType"),
                        "read": r.get("PermissionsRead"),
                        "create": r.get("PermissionsCreate"),
                        "edit": r.get("PermissionsEdit"),
                        "delete": r.get("PermissionsDelete"),
                        "view_all": r.get("PermissionsViewAllRecords"),
                        "modify_all": r.get("PermissionsModifyAllRecords")})
        return out


PROVIDERS = {"mock": MockProvider, "salesforce": SalesforceProvider}


def get_provider(source_id: str, kind: str | None = None) -> Provider:
    k = (kind or os.environ.get("SAAS_PROVIDER", "mock")).lower()
    if k not in PROVIDERS:
        raise SaasError(f"未知 provider：{k}（有 {sorted(PROVIDERS)}）")
    return PROVIDERS[k](source_id)


# ---------------------------------------------------------------- dump
def dump_permissions(source_id: str, kind: str | None = None,
                     require_attestation: bool = True) -> dict:
    """把权限现状取回来。**只有事实，没有结论。**

    没有只读担保就拒绝取数 —— 见模块文档：OAuth 证明不了只读。
    """
    att = readonly_attestation(source_id)
    if require_attestation and not att:
        raise SaasError(
            f"{source_id} 还没有只读担保。OAuth 的 `api` scope 全有全无，"
            f"证明不了连接用户是只读的 —— 需要 Owner 确认并留下可复核证据。")
    p = get_provider(source_id, kind)
    users = p.users()
    inv = {
        "source_id": source_id, "provider": p.name,
        "attestation": att,
        "users": users,
        "permission_sets": p.permission_sets(),
        "assignments": p.assignments(),      # 关系展开在控制面是允许的
        "object_access": p.object_access(),
        "sharing": p.sharing(),
    }
    inv["counts"] = {k: len(v) for k, v in inv.items() if isinstance(v, list)}
    inv["counts"]["inactive_users"] = sum(1 for u in users if not u.get("active"))
    return inv


def save_dump(inv: dict, out_dir="outputs/saas") -> str:
    p = pathlib.Path(out_dir) / f"{inv['source_id']}.permissions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)
