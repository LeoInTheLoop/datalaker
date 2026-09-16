"""Lossless MIME/flags/internaldate state over IMAP, not SMTP re-enactment."""
from __future__ import annotations

import base64
import imaplib
import json
import re
import urllib.request
from .bundle import SnapshotError


def _ok(result, operation):
    status, data = result
    if status != "OK":
        raise SnapshotError(f"IMAP {operation} failed: {status}")
    return data


def validate(value):
    if not isinstance(value, dict) or set(value) != {"accounts"} or not isinstance(value["accounts"], list):
        raise SnapshotError("mail artifact must declare accounts")
    addresses = set()
    for account in value["accounts"]:
        if (not isinstance(account, dict) or set(account) != {"address", "login", "folders"}
                or not isinstance(account["address"], str) or not account["address"]
                or account["address"] in addresses or not isinstance(account["folders"], list)
                or not isinstance(account["login"], str) or not account["login"]):
            raise SnapshotError("duplicate/invalid mail account")
        addresses.add(account["address"])
        folders = set()
        for folder in account["folders"]:
            if (not isinstance(folder, dict) or set(folder) != {"name", "subscribed", "messages"}
                    or not isinstance(folder["name"], str) or not folder["name"]
                    or folder["name"] in folders or not isinstance(folder["subscribed"], bool)
                    or not isinstance(folder["messages"], list)):
                raise SnapshotError("duplicate/invalid mailbox")
            folders.add(folder["name"])
            for row in folder["messages"]:
                if not isinstance(row, dict) or set(row) != {"uid", "flags", "internaldate", "mime"}:
                    raise SnapshotError("invalid mail record")
                base64.b64decode(row["mime"], validate=True)
                if (not isinstance(row["flags"], list) or any(not isinstance(flag, str)
                        or not re.fullmatch(r"[\\A-Za-z0-9_$.-]+", flag) for flag in row["flags"])
                        or not isinstance(row["internaldate"], str)
                        or not re.fullmatch(r'"[^"\r\n]+"', row["internaldate"])):
                    raise SnapshotError("invalid mail flags/date")


def _folder_names(lines):
    result = []
    for line in lines or []:
        if not line:
            continue
        match = re.fullmatch(rb'\((.*?)\) (?:"[^"]*"|NIL) (.+)', line)
        if not match:
            raise SnapshotError("unsupported IMAP LIST response")
        if b"\\Noselect" in match[1]:
            continue
        name = match[2].decode("ascii")  # IMAP modified UTF-7 stays ASCII on wire.
        if name.startswith('"'):
            name = name[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        result.append(name)
    return result


def _quoted(name):
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def users(host, api_port):
    """Enumerate every mailbox; a caller-supplied list cannot prove completeness."""
    with urllib.request.urlopen(f"http://{host}:{api_port}/api/user", timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, list) or any(not isinstance(u, dict) or not u.get("email")
            or not u.get("login") for u in value):
        raise SnapshotError("GreenMail did not return a complete user inventory")
    return sorted([{"address": u["email"], "login": u["login"]}
                   for u in value], key=lambda u: u["address"])


def create_users(host, api_port, accounts):
    existing = users(host, api_port)
    if existing:
        raise SnapshotError("GreenMail must have no users before restoring its inventory")
    for account in accounts:
        data = {"email": account["address"], "login": account["login"], "password": "snapshot"}
        request = urllib.request.Request(f"http://{host}:{api_port}/api/user", method="POST",
            data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise SnapshotError("GreenMail account creation failed")
    expected = sorted([{k: a[k] for k in ("address", "login")} for a in accounts], key=lambda u: u["address"])
    if users(host, api_port) != expected:
        raise SnapshotError("GreenMail account restore read-back mismatch")


def capture(host, port, accounts) -> dict:
    out = {"accounts": []}
    for account_info in sorted(accounts, key=lambda a: a["address"]):
        with imaplib.IMAP4(host, port, timeout=15) as conn:
            _ok(conn.login(account_info["login"], "snapshot"), "login")
            subscribed = set(_folder_names(_ok(conn.lsub(), "lsub")))
            account = {**account_info, "folders": []}
            for name in _folder_names(_ok(conn.list(), "list")):
                _ok(conn.select(_quoted(name), readonly=True), "select")
                ids = _ok(conn.uid("search", None, "ALL"), "search")[0].split()
                folder = {"name": name, "subscribed": name in subscribed, "messages": []}
                for uid in ids:
                    data = _ok(conn.uid("fetch", uid, "(UID FLAGS INTERNALDATE BODY.PEEK[])"), "fetch")
                    payload = next((item for item in data if isinstance(item, tuple)), None)
                    if not payload or not isinstance(payload[1], bytes):
                        raise SnapshotError("IMAP fetch omitted raw MIME")
                    head, raw = payload
                    date = re.search(rb'INTERNALDATE ("[^"]+")', head)
                    if not date:
                        raise SnapshotError("IMAP fetch omitted internal date")
                    folder["messages"].append({"uid": uid.decode(),
                        "flags": [f.decode() for f in imaplib.ParseFlags(head)],
                        "internaldate": date[1].decode(), "mime": base64.b64encode(raw).decode()})
                account["folders"].append(folder)
            out["accounts"].append(account)
    validate(out)
    return out


def restore(host, port, value) -> dict:
    """Destination server must be fresh; read back every message before release."""
    validate(value)
    for account in value["accounts"]:
        address = account["address"]
        with imaplib.IMAP4(host, port, timeout=15) as conn:
            _ok(conn.login(account["login"], "snapshot"), "login")
            existing = set(_folder_names(_ok(conn.list(), "list")))
            for folder in account["folders"]:
                name = folder["name"]
                if name not in existing:
                    _ok(conn.create(_quoted(name)), "create")
                _ok(conn.select(_quoted(name)), "select")
                if _ok(conn.search(None, "ALL"), "search")[0]:
                    raise SnapshotError("refusing to append snapshot onto a nonempty mailbox")
                for row in folder["messages"]:
                    flags = "(" + " ".join(f for f in row["flags"] if f != "\\Recent") + ")"
                    _ok(conn.append(_quoted(name), flags, row["internaldate"],
                                    base64.b64decode(row["mime"])), "append")
                _ok((conn.subscribe if folder["subscribed"] else conn.unsubscribe)(_quoted(name)), "subscription")
    actual = capture(host, port, [{k: a[k] for k in ("address", "login")} for a in value["accounts"]])
    def normalized(v):
        return sorted([{"address": a["address"], "folders": sorted([
            {**f, "messages": [{k: (sorted(x for x in v if x != "\\Recent") if k == "flags" else v)
                for k, v in m.items() if k != "uid"} for m in f["messages"]]}
            for f in a["folders"]], key=lambda f: f["name"])} for a in v["accounts"]], key=lambda a: a["address"])
    if normalized(actual) != normalized(value):
        raise SnapshotError("mail restore read-back mismatch")
    return {"accounts": len(actual["accounts"]),
            "messages": sum(len(f["messages"]) for a in actual["accounts"] for f in a["folders"])}
