"""Tiny dependency-free ``.env`` loader.

Reads ``KEY=VALUE`` pairs from a gitignored ``.env`` file (default: the repo
root's ``.env``) and merges them into :data:`os.environ` **without overriding**
variables that are already set — so the real environment always wins over a
local ``.env``. On the host that means systemd's ``EnvironmentFile``
(``/etc/default/amazon-wishlist``) is authoritative and a stray ``.env`` could
never shadow it; this loader only fills in keys that are not yet present, which
is exactly the convenience local development wants.

Supported syntax (deliberately small; see ``.env.example``):
- blank lines and ``#`` comments are skipped
- ``KEY=VALUE``, ``KEY = VALUE``, and ``export KEY=VALUE``
- a value wrapped in matching single or double quotes has the quotes stripped
  (quoting lets a value contain spaces or a leading ``#``)
- for unquoted values, a trailing `` # comment`` is dropped

It is a convenience, never a place for committed credentials: ``.env`` is
gitignored and must never be committed.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv", "default_dotenv_path"]


def default_dotenv_path() -> Path:
    """The repo-root ``.env`` (the parent of the ``app`` package)."""
    return Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: str | Path | None = None) -> int:
    """Merge ``.env`` into ``os.environ``; return the count of keys set.

    ``path`` defaults to the repo-root ``.env``. Keys already present in the
    live environment are left untouched. Missing file is a no-op (returns 0).
    """
    envpath = Path(path) if path is not None else default_dotenv_path()
    if not envpath.is_file():
        return 0

    loaded = 0
    with envpath.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            quoted = (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ("'", '"')
            )
            if quoted:
                value = value[1:-1]
            else:
                # Drop a trailing ` # comment` on an unquoted value.
                hash_pos = value.find(" #")
                if hash_pos != -1:
                    value = value[:hash_pos].rstrip()

            if not os.environ.get(key):
                os.environ[key] = value
                loaded += 1
    return loaded
