"""Browser-fingerprint rotation for anti-bot compliance.

When the live-deal verifier (``scripts/verify_deals.py``) opens a fresh Amazon
product page per book it wants the browser to look like an ordinary desktop
Chrome 149 user, not a fingerprinted bot. This module supplies the rotating
fingerprint dimensions — User-Agent, locale and viewport — and a helper that
produces a fingerprint differing from a previous one in *all three* fields
(used right after a VPN exit-IP rotation, so the IP and the fingerprint change
together). Pacing / jitter lives in the orchestrator, not here.

A note on realism: the four User-Agent strings each carry a distinct OS token
so the browser's ``navigator``/platform profile differs per rotation:

* ``_UA_LINUX``  → ``X11; Linux x86_64``
* ``_UA_WIN``    → ``Windows NT 10.0; Win64; x64``
* ``_UA_MAC``    → ``Macintosh; Intel Mac OS X 10_15_7``
* ``_UA_CROS``   → ``X11; CrOS x86_64``

``next_fingerprint()`` advances one deterministic counter per dimension, so
over ~200 calls it cycles every User-Agent, every locale and every viewport
(the combined (ua, locale, viewport) triple repeats every
lcm(4, 6, 5) = 60 calls). ``fresh_fingerprint()`` is the post-rotation variant:
it returns a fingerprint whose UA, locale and viewport are each different from
the one passed in (or the last one generated).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Fingerprint",
    "USER_AGENTS",
    "LOCALES",
    "VIEWPORTS",
    "next_fingerprint",
    "fresh_fingerprint",
    "reset_rotation",
]

# --- User-Agents: plain desktop Chrome 149, one per distinct OS profile ---- #
_UA_LINUX = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_UA_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_UA_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_UA_CROS = (
    "Mozilla/5.0 (X11; CrOS x86_64 15000.0.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

USER_AGENTS = [_UA_LINUX, _UA_WIN, _UA_MAC, _UA_CROS]

# --- Locales (English variants) and viewports (WxH) ------------------------ #
LOCALES = ["en-US", "en-GB", "en-CA", "en-AU", "en-IE", "en-NZ"]
VIEWPORTS = ["1920x1080", "1536x864", "1728x1117", "1366x768", "2560x1440"]


@dataclass(frozen=True)
class Fingerprint:
    """One browser's outward-looking identity for a single page load."""

    user_agent: str
    locale: str
    width: int
    height: int


def _viewport_dims(viewport: str) -> tuple[int, int]:
    w, h = viewport.lower().split("x")
    return int(w), int(h)


def _compose(user_agent: str, locale: str, viewport: str) -> Fingerprint:
    width, height = _viewport_dims(viewport)
    return Fingerprint(user_agent=user_agent, locale=locale, width=width, height=height)


# Module-level rotation state (single-process verifier).
_state = {"ua": 0, "locale": 0, "viewport": 0}
_last: Fingerprint | None = None


def reset_rotation() -> None:
    """Reset the deterministic counters and the "last" fingerprint."""
    global _last
    _state["ua"] = 0
    _state["locale"] = 0
    _state["viewport"] = 0
    _last = None


def next_fingerprint() -> Fingerprint:
    """Return the next fingerprint, advancing each dimension by one.

    Deterministic: each of the UA / locale / viewport counters steps forward on
    every call (wrapping), so repeated calls walk the three lists in lockstep.
    """
    global _last
    user_agent = USER_AGENTS[_state["ua"] % len(USER_AGENTS)]
    locale = LOCALES[_state["locale"] % len(LOCALES)]
    viewport = VIEWPORTS[_state["viewport"] % len(VIEWPORTS)]
    fp = _compose(user_agent, locale, viewport)
    _state["ua"] += 1
    _state["locale"] += 1
    _state["viewport"] += 1
    _last = fp
    return fp


def _first_different(options, current: str) -> str:
    """The first element of ``options`` that differs from ``current``."""
    for item in options:
        if item != current:
            return item
    return options[0]


def fresh_fingerprint(fp: Fingerprint | None = None) -> Fingerprint:
    """A fingerprint differing from ``fp`` (or the last generated) in ALL fields.

    Used after a NordVPN exit-IP rotation so the IP *and* the UA/locale/viewport
    change together. ``fp`` defaults to the most recently produced fingerprint.
    """
    global _last
    base = fp if fp is not None else _last
    if base is None:
        return next_fingerprint()

    user_agent = _first_different(USER_AGENTS, base.user_agent)
    locale = _first_different(LOCALES, base.locale)
    viewport = _first_different(VIEWPORTS, f"{base.width}x{base.height}")

    # Continue the rotation from just after the chosen indices so a subsequent
    # next_fingerprint() does not immediately repeat this one.
    _state["ua"] = USER_AGENTS.index(user_agent) + 1
    _state["locale"] = LOCALES.index(locale) + 1
    _state["viewport"] = VIEWPORTS.index(viewport) + 1

    fresh = _compose(user_agent, locale, viewport)
    _last = fresh
    return fresh
