"""Versioned, private cold checkpoints. A bundle contains state, never a script.

The Docker adapter owns quiescence. This module validates the whole checkpoint
before any destination is touched. Empty artifacts are explicit; missing ones
are errors. Raw bundles may contain database passwords and private messages.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tarfile
import math

VERSION = 1
PARTS = {"postgres", "objects", "iceberg", "runtime", "mail", "deployment"}
ARCHIVES = {"postgres", "objects", "iceberg", "runtime"}


class SnapshotError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


def inventory(path: Path) -> dict:
    """Content + modes + ownership, independent of archive order/mtime headers."""
    result = {}
    try:
        with tarfile.open(path) as archive:
            for entry in archive:
                name = entry.name.removeprefix("./").rstrip("/")
                if name in ("", "."):
                    continue
                if name.startswith("/") or ".." in Path(name).parts or name in result:
                    raise SnapshotError(f"unsafe/duplicate archive entry: {name}")
                if not (entry.isfile() or entry.isdir()):
                    # External tablespaces/symlinked HOME need an explicit adapter;
                    # silently losing them would turn this into a partial snapshot.
                    raise SnapshotError(f"checkpoint contains non-local file: {name}")
                item = {"mode": entry.mode, "uid": entry.uid, "gid": entry.gid,
                        "type": "file" if entry.isfile() else "directory"}
                if entry.isfile():
                    with archive.extractfile(entry) as stream:
                        item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
                    item["size"] = entry.size
                result[name] = item
    except (tarfile.TarError, OSError) as exc:
        raise SnapshotError(f"unreadable archive: {path.name}: {exc}") from exc
    return result


def seal(root: Path, *, captured_at: float, metadata: dict) -> dict:
    artifacts = {}
    for part in sorted(PARTS):
        file = part + (".tar" if part in ARCHIVES else ".json")
        path = root / file
        artifacts[part] = {"file": file, "sha256": sha256(path), "bytes": path.stat().st_size}
        if part in ARCHIVES:
            artifacts[part]["inventory"] = inventory(path)
    value = {"version": VERSION, "captured_at": captured_at,
             "clock": {"policy": "absolute", "unit": "unix_seconds"},
             "consistency": "cold", "artifacts": artifacts, "metadata": metadata}
    write_json(root / "manifest.json", value)
    validate(root)
    return value


def validate(root: Path) -> dict:
    root = root.resolve()
    try:
        value = json.loads((root / "manifest.json").read_text())
        if set(value) != {"version", "captured_at", "clock", "consistency", "artifacts", "metadata"}:
            raise SnapshotError("unknown or missing manifest fields")
        if value["version"] != VERSION or value["consistency"] != "cold":
            raise SnapshotError("unsupported checkpoint version/consistency")
        if value["clock"] != {"policy": "absolute", "unit": "unix_seconds"}:
            raise SnapshotError("unsupported clock policy; deadlines must not be silently rewritten")
        if (isinstance(value["captured_at"], bool) or not isinstance(value["captured_at"], (int, float))
                or not math.isfinite(value["captured_at"]) or value["captured_at"] <= 0):
            raise SnapshotError("invalid capture time")
        if (not isinstance(value["metadata"], dict)
                or set(value["metadata"]) != {"adapter", "project", "origin"}
                or value["metadata"]["adapter"] != "datalaker-compose-v1"
                or not str(value["metadata"]["origin"]).startswith("snapshot:")):
            raise SnapshotError("invalid checkpoint provenance")
        if set(value["artifacts"]) != PARTS:
            raise SnapshotError("checkpoint must declare all six artifacts")
        for part, descriptor in value["artifacts"].items():
            expected = {"file", "sha256", "bytes"} | ({"inventory"} if part in ARCHIVES else set())
            if set(descriptor) != expected:
                raise SnapshotError(f"invalid descriptor: {part}")
            if descriptor["file"] != part + (".tar" if part in ARCHIVES else ".json"):
                raise SnapshotError(f"invalid artifact path: {part}")
            path = root / descriptor["file"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != descriptor["bytes"]:
                raise SnapshotError(f"missing/changed artifact: {part}")
            if sha256(path) != descriptor["sha256"]:
                raise SnapshotError(f"checksum mismatch: {part}")
            if part in ARCHIVES and inventory(path) != descriptor["inventory"]:
                raise SnapshotError(f"inventory mismatch: {part}")
        from .mailbox import validate as validate_mail
        validate_mail(json.loads((root / "mail.json").read_text()))
        from .docker_backend import SERVICES
        deployment = json.loads((root / "deployment.json").read_text())
        if set(deployment) != set(SERVICES) or any(
                not isinstance(d, dict) or set(d) != {"image", "environment"}
                or not isinstance(d["image"], str) or not isinstance(d["environment"], dict)
                for d in deployment.values()):
            raise SnapshotError("invalid deployment artifact")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(f"invalid checkpoint: {exc}") from exc


def create_directory(path: Path) -> None:
    # Never append to an old/partially written checkpoint.
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
