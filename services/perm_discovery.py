"""权限现状发现（readme 11.2）—— 只读、只建议，结果写进 Remediation Ledger。

**只观测不修改**（铁律 4）。在 SaaS 上这条比在数据库上更要紧：
CRM 的权限写接口往往和业务配置共用一套 API，一次错误的 Profile 变更
能让整个销售团队当场登不进去，爆炸半径比数据库大。

## 两侧的分量不一样

20–50 人公司通常只有一两个人碰数据库，但 CRM 是全员每天在用的。
「销售能一键导出全部客户名单」「离职同事账号还挂着管理员」——
**SaaS 侧的发现往往更有说服力，因为它指向真实会发生的泄露路径**，
不是理论上的过度授权。

## 一个必须诚实的地方

「过度授权 = 授了但从不访问」需要**逐用户访问记录**。
Postgres 默认不采集这个（要 `pg_stat_statements` + 额外配置），
Salesforce 的登录历史也只覆盖登录不覆盖对象访问。
拿不到就写 `access_evidence="unknown"`，**不编造「近 90 天仅 6 人访问」这种数字**。
没有证据的指控推不动整改，只会消耗信任。
"""
import time

# 敏感表的判定：先用命名约定，接上 catalog 后换成 classification（11.3）
SENSITIVE_HINTS = ("salary", "payroll", "invoice", "customer", "contact",
                   "phone", "email", "hr_", "fin_", "person", "employee")
WRITE_PRIVS = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}


def _sensitive(table: str) -> bool:
    t = (table or "").lower()
    return any(h in t for h in SENSITIVE_HINTS)


def _f(kind, severity, subject, detail, **kw):
    return {"issue_type": kind, "severity": severity, "subject": subject,
            "detail": detail, "access_evidence": "unknown", **kw}


# ---------------------------------------------------------------- 数据库侧
def scan_database(source_id: str) -> dict:
    """扫一个数据库源的权限现状。走元数据通道（允许在系统目录上 join）。"""
    import connector

    grants = connector.list_grants(source_id)
    roles = {r["role"]: r for r in connector.list_roles(source_id)}
    findings = []

    # 1. PUBLIC 授权 —— 等于全员可读，最常见也最容易出成果的发现
    pub = [g for g in grants if g["grantee"].upper() == "PUBLIC"]
    for g in pub:
        findings.append(_f(
            "public_grant", "high", f"{source_id}.{g['table']}",
            f"授予 PUBLIC 的 {g['privilege']} —— 任何能连上库的人都拿得到",
            suggestion=f"回收 PUBLIC 的 {g['privilege']}，改为按角色授予"))

    # 2. 敏感表上的写权限
    for g in grants:
        if g["privilege"] in WRITE_PRIVS and _sensitive(g["table"]):
            findings.append(_f(
                "write_on_sensitive", "high",
                f"{source_id}.{g['table']}",
                f"{g['grantee']} 对敏感表持有 {g['privilege']}",
                suggestion=f"确认 {g['grantee']} 是否真的需要写；只读需求应降为 SELECT"))

    # 3. 可转授（WITH GRANT OPTION）—— 权限会自己扩散
    for g in grants:
        if g["grantable"]:
            findings.append(_f(
                "grantable", "medium", f"{source_id}.{g['table']}",
                f"{g['grantee']} 可以把 {g['privilege']} 再转授给别人",
                suggestion="去掉 GRANT OPTION，授权应当集中"))

    # 4. 超级用户与绕过行级安全
    for name, r in roles.items():
        if r["superuser"]:
            findings.append(_f(
                "superuser", "medium", f"{source_id}:{name}",
                "超级用户 —— 所有表所有权限，且绕过一切策略",
                suggestion="日常连接不应使用超级用户"))
        if r["bypass_rls"]:
            findings.append(_f(
                "bypass_rls", "medium", f"{source_id}:{name}",
                "可绕过行级安全策略", suggestion="确认是否必要"))

    # 5. 授了权但登录不了 —— 大多是历史遗留
    granted = {g["grantee"] for g in grants}
    for name in granted:
        r = roles.get(name)
        if r and not r["can_login"] and not r["member_of"]:
            findings.append(_f(
                "orphan_grant", "low", f"{source_id}:{name}",
                "该角色有表权限但无法登录、也没有成员 —— 疑似历史遗留",
                suggestion="确认后回收"))

    return {"source_id": source_id, "kind": "database",
            "grants": len(grants), "roles": len(roles),
            "findings": findings,
            "note": "访问频率未采集（需 pg_stat_statements），"
                    "因此不判定「授了但从不访问」"}


# ---------------------------------------------------------------- SaaS 侧
STALE_LOGIN_DAYS = 90
ADMIN_HINTS = ("admin", "administrator", "break_glass", "modify_all")


def scan_saas(source_id: str, inventory: dict | None = None,
              now: float | None = None) -> dict:
    """扫一个 SaaS 源的权限现状。输入是 8.2 控制面 dump 出来的归一结构。"""
    import saas
    inv = inventory or saas.dump_permissions(source_id)
    t = now or time.time()
    findings = []

    # 1. 非管理员 Profile 带 Modify All Data —— 最典型的越界
    for o in inv.get("object_access", []):
        who = (o.get("profile_or_set") or "")
        if o.get("modify_all") and not any(h in who.lower() for h in ADMIN_HINTS):
            findings.append(_f(
                "modify_all_on_non_admin", "high", f"{source_id}:{who}",
                f"{who} 对 {o.get('object')} 持有 Modify All Data",
                suggestion=f"收回 {who} 的 Modify All Data，改用记录级共享"))
        if o.get("view_all") and not any(h in who.lower() for h in ADMIN_HINTS):
            findings.append(_f(
                "view_all_on_non_admin", "medium", f"{source_id}:{who}",
                f"{who} 可查看 {o.get('object')} 的全部记录",
                suggestion="确认是否需要全量可见，否则按团队/地域收敛"))

    # 2. 长期未登录却仍 Active 的账号，尤其带管理员的
    admins = {a["user_id"] for a in inv.get("assignments", [])
              if any(h in (a.get("permission_set") or "").lower()
                     for h in ADMIN_HINTS)}
    for u in inv.get("users", []):
        if not u.get("active"):
            continue
        last = u.get("last_login") or ""
        stale = False
        if last:
            try:
                import datetime
                d = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
                stale = (t - d.timestamp()) > STALE_LOGIN_DAYS * 86400
            except Exception:                                # noqa: BLE001
                stale = False
        if stale:
            sev = "high" if u["id"] in admins else "medium"
            findings.append(_f(
                "stale_active_account", sev, f"{source_id}:{u.get('email')}",
                f"{u.get('name')} 距上次登录超过 {STALE_LOGIN_DAYS} 天但账号仍 Active"
                + ("，且持有管理员权限集" if u["id"] in admins else ""),
                suggestion="确认是否离职；离职账号应停用而不是留着",
                access_evidence=f"last_login={last}"))

    # 3. 外部域账号持有敏感权限
    for a in inv.get("assignments", []):
        u = next((x for x in inv.get("users", []) if x["id"] == a["user_id"]), None)
        email = (u or {}).get("email", "")
        if email and not email.endswith("@acme.com"):
            findings.append(_f(
                "external_account_privileged", "high", f"{source_id}:{email}",
                f"外部域账号持有权限集 {a.get('permission_set')}",
                suggestion="外包/合作方账号应设期限并按最小范围授予"))

    # 4. 组织级默认共享过宽
    for sh in inv.get("sharing", []):
        owd = (sh.get("org_wide_default") or "")
        if "Read/Write" in owd or owd == "Public Full Access":
            findings.append(_f(
                "org_wide_open", "high", f"{source_id}:{sh.get('object')}",
                f"{sh.get('object')} 的组织级默认是「{owd}」——全公司可改",
                suggestion="改为 Private 或 Public Read Only，按需用共享规则放开"))

    return {"source_id": source_id, "kind": "saas",
            "provider": inv.get("provider"), "users": len(inv.get("users", [])),
            "findings": findings,
            "note": "对象级访问记录未采集，因此不判定「授了但从不访问」"}


# ---------------------------------------------------------------- 写台账
def to_ledger(scan: dict, owner_role: str | None = None) -> list:
    """把发现写进 Remediation Ledger。**只建议，不执行。**"""
    import ledger
    out = []
    for f in scan["findings"]:
        r = ledger.record(f["subject"], f["issue_type"], category="permission",
                          observed_pattern=f["detail"],
                          action_taken="仅观测，未对源系统做任何变更",
                          owner_role=owner_role)
        if f.get("suggestion"):
            ledger.propose(r["rl_id"], f["suggestion"],
                           owner_role or "owner", due_days=14)
        out.append({**r, "issue_type": f["issue_type"],
                    "severity": f["severity"]})
    return out
