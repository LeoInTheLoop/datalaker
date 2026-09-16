"""The recipient resolution shared by the real tools and Snapshot verification."""


def resolve_to(store, role: str) -> str:
    import notify
    who = (store.resolve_role(role) if store else None) or ""
    if "@" in who:
        return who
    for key in (f"MAIL_{role.upper().replace(':', '_')}", "MAIL_SPONSOR", "MAIL_OWNER"):
        value = notify.cfg(key, "")
        if "@" in value:
            return value
    return ""
