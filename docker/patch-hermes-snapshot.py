"""Small, checked compatibility patch against the pinned Hermes revision.

Opt-in restores process pre-existing UNSEEN mail; ordinary startup keeps Hermes'
original mark-all-existing behavior. No synthetic messages or prompt injection.
"""
from pathlib import Path
import sys


def patch(root):
    path = Path(root) / "plugins/platforms/email/adapter.py"
    source = path.read_text()
    original = 'status, data = imap.uid("search", None, "ALL")'
    replacement = ('status, data = imap.uid("search", None, '
                   '"SEEN" if os.environ.get("HERMES_EMAIL_RESTORE_UNSEEN") == "1" else "ALL")')
    if replacement in source:
        return
    if source.count(original) != 1 or "import os" not in source:
        raise RuntimeError("Hermes email adapter changed; review Snapshot restore compatibility")
    path.write_text(source.replace(original, replacement))


if __name__ == "__main__":
    patch(sys.argv[1])
