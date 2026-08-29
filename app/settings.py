"""Runtime-persisted settings (the Settings tab).

Stored as key/value rows in the wishlist DB's ``settings`` table (created by
``db.init_db`` / ``_migrate``). Configuration env vars remain the DEFAULTS; a
setting present here overrides them at the point of use (the scheduler's daily
scrape/BookBub times, the BookBub Deals tab's default cover size). Settings
are per-instance, primary-only preferences — ``sync.export_catalog`` never
touches the table, so they do not mirror to a secondary.

All access goes through :func:`db.connect`, which reads the module-level
``db.DB_PATH`` at call time — so the smoke test's ``db.DB_PATH`` patch
redirects these queries exactly like every other one.
"""

from __future__ import annotations

from .db import connect

__all__ = ["get", "get_int", "set", "all"]


def get(key: str, default: str | None = None) -> str | None:
    """The stored value for ``key`` as a raw string, or ``default`` when
    unset (or blank)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None or row["value"] in (None, ""):
        return default
    return row["value"]


def get_int(key: str, default: int | None = None) -> int | None:
    """The stored value parsed as an int; ``default`` when unset, blank, or
    not a valid integer (a hand-edited/corrupt value must not crash a caller).
    """
    v = get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def set(key: str, value: str | int | None) -> None:
    """Store ``value`` for ``key`` (upsert; stored as text). ``None`` deletes
    the row — a reset back to the env/config default."""
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def all() -> dict[str, str]:
    """Every stored setting as ``{key: value}`` (empty dict when none)."""
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
