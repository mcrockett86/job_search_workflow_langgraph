"""LinkedIn session import — sign in on a real browser, hand the server only cookies.

Why: the old sign-in relayed a headless browser to the viewer (screenshots
out, clicks back). It was laggy, its coordinate math kept drifting, and 2FA
through it was unworkable. This module deletes that whole problem class:
the interactive part (password, 2FA, push, passkey, Cloudflare) happens in
a REAL Chrome window on the user's own machine — see
``agents/linkedin_login.py --pair`` — and the server receives only the
resulting session cookies, which it injects into the profile's persistent
Chrome dir. A LinkedIn session cookie is not IP-bound, so a session started
on the Mac works from the VPS exactly like a laptop user signing in from
home and then the office.

Security contract:
- Cookies are password-equivalent. They are held in memory only, injected
  into Chrome via CDP (which handles httpOnly ``li_at``), and never appear
  in any response body, log line, error message, or on disk outside the
  profile dir.
- A claim needs the single-use pairing code AND the viewer's own
  authentication (Caddy basic-auth sits in front of every route).
- The manual paste path accepts only the ``li_at`` value (the session
  cookie LinkedIn needs); junk is rejected without echoing it back.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time

logger = logging.getLogger(__name__)

PAIR_TTL = 600            # a pairing code lives 10 minutes
CLAIM_WINDOW = 900        # ...and may be claimed for 15 min after that
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L
_LI_AT_RE = re.compile(r"^[A-Za-z0-9+/_=.\-]{20,512}$")

# The cookies that make a LinkedIn session. li_at is the session token;
# the rest ride along when the helper has them (they improve fidelity but
# are optional).
_COOKIE_DOMAIN = ".linkedin.com"
_OPTIONAL_NAMES = ("JSESSIONID", "lidc", "bcookie", "lang", "fsb", "visit",
                   "liap")


class _Pair:
    __slots__ = ("code", "slug", "created_at", "state", "result", "lock")

    def __init__(self, code: str, slug: str) -> None:
        self.code = code
        self.slug = slug
        self.created_at = time.time()
        self.state = "pending"     # pending | claimed | expired
        self.result: dict | None = None
        self.lock = threading.Lock()


_PAIRS: dict[str, _Pair] = {}     # code -> pair
_PAIRS_BY_SLUG: dict[str, str] = {}   # slug -> active code
_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _gc_locked() -> None:
    """Drop codes past their whole window (caller holds _LOCK)."""
    dead = [c for c, p in _PAIRS.items()
            if _now() - p.created_at > PAIR_TTL + CLAIM_WINDOW]
    for c in dead:
        p = _PAIRS.pop(c)
        if _PAIRS_BY_SLUG.get(p.slug) == c:
            _PAIRS_BY_SLUG.pop(p.slug, None)


def start_pair(slug: str) -> dict:
    """Create a single-use pairing code for *slug* (replaces any pending one)."""
    with _LOCK:
        _gc_locked()
        old = _PAIRS_BY_SLUG.get(slug)
        if old and old in _PAIRS and _PAIRS[old].state == "pending":
            _PAIRS.pop(old)
            _PAIRS_BY_SLUG.pop(slug, None)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        pair = _Pair(code, slug)
        _PAIRS[code] = pair
        _PAIRS_BY_SLUG[slug] = code
    return {"code": code, "slug": slug, "expires_in": PAIR_TTL}


def _find_pair(code: str) -> _Pair | None:
    code = (code or "").strip().upper()
    with _LOCK:
        _gc_locked()
        return _PAIRS.get(code)


def pair_status(code: str) -> dict | None:
    pair = _find_pair(code)
    if pair is None:
        return None
    with pair.lock:
        return {"state": pair.state, "slug": pair.slug,
                "result": dict(pair.result) if pair.result else None}


def claim_pair(code: str, cookies: dict[str, str]) -> dict:
    """Consume a pairing code and install *cookies* for its profile.

    Returns {ok, ...} — never any cookie material. Raises ValueError with a
    user-safe reason when the code is bad/expired/used or the cookies are.
    """
    pair = _find_pair(code)
    if pair is None:
        raise ValueError("Pairing code not found — start a new sign-in in the viewer.")
    with pair.lock:
        if pair.state == "claimed":
            raise ValueError("That pairing code was already used.")
        if _now() - pair.created_at > PAIR_TTL + CLAIM_WINDOW:
            pair.state = "expired"
            raise ValueError("That pairing code expired — start a new sign-in.")
        pair.state = "claimed"
    try:
        clean = _validate_cookies(cookies)
        ok, message = install_session(pair.slug, clean)
    except ValueError:
        # A bad upload shouldn't burn the code — let them fix and retry.
        with pair.lock:
            if pair.state == "claimed":
                pair.state = "pending"
        raise
    with pair.lock:
        pair.result = {"ok": ok, "message": message}
    if ok:
        logger.info("LinkedIn session imported for profile %s", pair.slug)
    return {"ok": ok, "message": message}


def _validate_cookies(cookies: object) -> dict[str, str]:
    if not isinstance(cookies, dict):
        raise ValueError("Expected a cookie object.")
    out: dict[str, str] = {}
    li_at = str(cookies.get("li_at") or "").strip()
    if not _LI_AT_RE.match(li_at):
        raise ValueError("No usable li_at session cookie in the upload.")
    out["li_at"] = li_at
    for name in _OPTIONAL_NAMES:
        val = str(cookies.get(name) or "").strip()
        if val and len(val) <= 512 and re.match(r"^[A-Za-z0-9%+/_=.\- ]+$", val):
            out[name] = val
    return out


# ---------------------------------------------------------------------------
# Install + verify: drive the profile's persistent Chrome via CDP
# ---------------------------------------------------------------------------
VERIFY_BUDGET = 15.0   # seconds /feed must hold logged-in after injection


def _verify_feed(driver, budget: float | None = None) -> bool:
    """Strict post-injection check: /feed must STAY logged in.

    ls.session_valid() sleeps a flat 3 s, which false-positives right after
    cookie injection: LinkedIn's redirect to /authwall (bad cookie) can take
    longer than that to fire, so the URL still reads /feed. Here we poll for
    `budget` seconds and require the logged-in state to hold at the end.
    """
    import time as _t
    from . import linkedin_scraper as ls
    budget = VERIFY_BUDGET if budget is None else budget
    interval = max(0.05, min(1.0, budget / 8))
    driver.get("https://www.linkedin.com/feed/")
    deadline = _t.time() + budget
    while _t.time() < deadline:
        try:
            url = driver.current_url or ""
        except Exception:  # noqa: BLE001 — mid-navigation
            url = ""
        if ls.is_logged_out_url(url):
            return False
        _t.sleep(interval)
    try:
        url = driver.current_url or ""
    except Exception:  # noqa: BLE001
        return False
    return "linkedin.com" in url and not ls.is_logged_out_url(url)


def install_session(slug: str, cookies: dict[str, str]) -> tuple[bool, str]:
    """Inject *cookies* into the profile's Chrome dir and verify /feed.

    The profile dir is wiped first so an old person's session can never
    linger under a new one (this is the 'sign in as jen2' path).
    """
    from . import profiles
    from . import linkedin_scraper as ls

    p = profiles.get(slug)
    p.ensure_dirs()
    import shutil
    shutil.rmtree(p.linkedin_profile_dir, ignore_errors=True)
    os.makedirs(p.linkedin_profile_dir, exist_ok=True)

    driver = None
    try:
        driver = ls._setup_driver(headless=True, profile_dir=p.linkedin_profile_dir)  # noqa: SLF001
        driver.set_window_size(1280, 900)
        # CDP needs a page on the right origin before Network.setCookie.
        driver.get("https://www.linkedin.com")
        for name, value in cookies.items():
            driver.execute_cdp_cmd("Network.setCookie", {
                "name": name, "value": value, "domain": _COOKIE_DOMAIN,
                "path": "/", "secure": True, "httpOnly": True,
                "sameSite": "None",
            })
        ok = _verify_feed(driver)
    except Exception as exc:  # noqa: BLE001 — surfaced without cookie material
        logger.exception("session install failed for %s", slug)
        return False, f"Could not install the session ({type(exc).__name__}). Try again."
    finally:
        if driver is not None:
            try:
                driver.quit()   # flushes the injected cookies to disk
            except Exception:  # noqa: BLE001
                pass
    if ok:
        return True, ("Signed in — LinkedIn accepted the session for "
                      f"{slug}. Scans can now run unattended.")
    return False, ("LinkedIn rejected that session (expired or revoked). "
                   "Sign in again or paste a fresh li_at.")


def paste_session(slug: str, li_at: str) -> tuple[bool, str]:
    """Manual fallback: the user pastes li_at copied from their own browser."""
    return install_session(slug, _validate_cookies({"li_at": li_at}))


def session_state(slug: str) -> dict:
    """Cheap, non-driving status for the viewer chip: dir has content?"""
    from . import profiles
    try:
        p = profiles.get(slug)
    except (KeyError, ValueError):
        return {"state": "unknown"}
    d = p.linkedin_profile_dir
    try:
        has = os.path.isdir(d) and bool(os.listdir(d))
    except OSError:
        has = False
    return {"state": "saved" if has else "none"}


def pending_pair(slug: str) -> dict | None:
    with _LOCK:
        _gc_locked()
        code = _PAIRS_BY_SLUG.get(slug)
        if not code or code not in _PAIRS:
            return None
        pair = _PAIRS[code]
    with pair.lock:
        if pair.state != "pending":
            return None
        return {"code": pair.code,
                "expires_in": max(0, int(PAIR_TTL - (_now() - pair.created_at)))}
