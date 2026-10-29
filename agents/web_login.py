"""Interactive LinkedIn sign-in relayed through the web viewer.

Why: on a headless VPS there is no screen to sign LinkedIn into. This module
runs a real (headless) Chrome session on LinkedIn's OWN login page and
streams screenshots to the viewer; the user types their credentials into
our small form and we type them into LinkedIn's page. The password is held
in memory for the length of one action — never logged, never written to
disk, never returned by any endpoint. What persists is only the session
cookies Chrome saves inside the *search profile's* directory, so later
scans run unattended.

Each search profile (mike, jen, …) gets its own login session and its own
Chrome profile dir, so one person's LinkedIn account is never used for
another's scans.

The worker thread owns the WebDriver; HTTP handlers only enqueue actions
and read state, so the driver is never touched from two threads.
"""

from __future__ import annotations

import base64
import logging
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Tunables (tests shrink the poll interval).
POLL_SECONDS = 1.5
TOTAL_TIMEOUT = 600          # hard cap for one sign-in session
IDLE_TIMEOUT = 300           # drop the browser if the user walks away
WINDOW = (1280, 900)

# Test seam: (profile_dir) -> WebDriver. Monkeypatched in unit tests.
def _make_driver(profile_dir: str):
    from . import linkedin_scraper as ls
    return ls._setup_driver(headless=True, profile_dir=profile_dir)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Two-factor verification methods LinkedIn may offer on its checkpoint page.
# Each method is located by element id first (LinkedIn's stable ids), then by
# visible text as a layout-change fallback.
# ---------------------------------------------------------------------------
TWOFA_METHODS: dict[str, dict] = {
    "app": {
        "label": "Authenticator app",
        "ids": ("#totp-auth", "#app-to-phone-totp-auth",
                "#authenticator-app-auth", "#totp-auth-label"),
        "phrases": ("authenticator", "verification code from an app"),
    },
    "sms": {
        "label": "Text message (SMS)",
        "ids": ("#app-to-phone-sms-auth", "#register-phone-auth",
                "#sms-auth", "#app-to-phone-sms-auth-label"),
        "phrases": ("text message", "text me", "sms"),
    },
    "email": {
        "label": "Email",
        "ids": ("#app-to-phone-email-auth", "#email-app-login",
                "#email-auth", "#app-to-phone-email-auth-label"),
        "phrases": ("email",),
    },
    "push": {
        "label": "LinkedIn app notification",
        "ids": ("#push-notifications-auth", "#push-auth",
                "#push-notifications-auth-label"),
        "phrases": ("mobile app", "notification", "push"),
    },
}

# Visible controls scanned for the text fallback.
_SCAN_SELECTOR = "button, a, label, span[role='button'], input[type='submit']"
# Buttons that submit the chosen method once selected.
_CONTINUE_TEXTS = ("next", "send", "submit", "continue", "get a code")


def _visible_texts(driver) -> list:
    from selenium.webdriver.common.by import By
    out = []
    try:
        els = driver.find_elements(By.CSS_SELECTOR, _SCAN_SELECTOR)
    except Exception:  # noqa: BLE001 — odd page variant
        return out
    for el in els:
        try:
            if el.is_displayed():
                out.append(el)
        except Exception:  # noqa: BLE001 — stale node
            continue
    return out


def _element_text(el) -> str:
    try:
        txt = (el.text or "").strip().lower()
    except Exception:  # noqa: BLE001
        txt = ""
    if not txt:
        try:
            txt = (el.get_attribute("aria-label") or "").strip().lower()
        except Exception:  # noqa: BLE001
            txt = ""
    if not txt:
        try:
            txt = (el.get_attribute("value") or "").strip().lower()
        except Exception:  # noqa: BLE001
            txt = ""
    return txt


def _find_twofa_option(driver, cfg: dict):
    """Locate the visible control that selects a 2FA method (ids, then text)."""
    from selenium.webdriver.common.by import By
    for sel in cfg["ids"]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:  # noqa: BLE001
            continue
        for el in els:
            try:
                if el.is_displayed():
                    return el
            except Exception:  # noqa: BLE001
                continue
    for el in _visible_texts(driver):
        txt = _element_text(el)
        if txt and any(p in txt for p in cfg["phrases"]):
            return el
    return None


def _on_twofa_page(url: str) -> bool:
    """True on LinkedIn's verification/checkpoint pages (never the plain
    login form, whose 'Email or phone' label would trip the text fallback)."""
    u = url or ""
    return "checkpoint" in u or "/auth/" in u


def detect_twofa(driver) -> dict[str, str]:
    """Map of method-key -> label for every 2FA option visible on the page.

    Cheap by design: runs every poll tick, so id lookups come first and the
    text scan only runs when no id matched.
    """
    if not _on_twofa_page(getattr(driver, "current_url", "") or ""):
        return {}
    found: dict[str, str] = {}
    for key, cfg in TWOFA_METHODS.items():
        if _find_twofa_option(driver, cfg) is not None:
            found[key] = cfg["label"]
    return found


def _click_continue(driver) -> None:
    """Best-effort: press the page's Next/Send/Submit after a method pick."""
    for el in _visible_texts(driver):
        txt = _element_text(el)
        if txt in _CONTINUE_TEXTS:
            try:
                el.click()
                return
            except Exception:  # noqa: BLE001 — LinkedIn may auto-submit
                return


class LoginSession:
    def __init__(self, slug: str, profile_dir: str) -> None:
        self.slug = slug
        self.profile_dir = profile_dir
        self.actions: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.screenshot_b64: str | None = None
        self.url: str = ""
        self.status = "starting"   # starting | waiting | success | failed | cancelled
        self.message = "Opening LinkedIn sign-in page…"
        self.twofa_options: dict[str, str] = {}   # method key -> label
        self.twofa_selected: str | None = None    # method key the user picked
        self.created_at = time.time()
        self.last_action_at = time.time()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def touch(self) -> None:
        self.last_action_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "url_host": _host(self.url),
                "screenshot": self.screenshot_b64,
                "twofa_options": dict(self.twofa_options),
                "twofa_selected": self.twofa_selected,
                "elapsed": round(time.time() - self.created_at),
            }


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:  # noqa: BLE001
        return ""


SESSIONS: dict[str, LoginSession] = {}
SESSIONS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def start(slug: str, profile_dir: str) -> LoginSession:
    """Begin (or return the live) sign-in session for *slug*."""
    with SESSIONS_LOCK:
        existing = SESSIONS.get(slug)
        if existing and existing.thread and existing.thread.is_alive():
            with existing.lock:
                if existing.status in ("starting", "waiting"):
                    return existing
        _reap_locked()
        sess = LoginSession(slug, profile_dir)
        SESSIONS[slug] = sess
        sess.thread = threading.Thread(target=_worker, args=(sess,),
                                       daemon=True,
                                       name=f"linkedin-login-{slug}")
        sess.thread.start()
        return sess


def get(slug: str) -> LoginSession | None:
    with SESSIONS_LOCK:
        return SESSIONS.get(slug)


def cancel(slug: str) -> bool:
    with SESSIONS_LOCK:
        sess = SESSIONS.get(slug)
    if not sess:
        return False
    sess.stop.set()
    sess.actions.put(("cancel", {}))
    return True


def enqueue(slug: str, action: str, payload: dict) -> LoginSession | None:
    with SESSIONS_LOCK:
        sess = SESSIONS.get(slug)
    if sess:
        sess.touch()
        sess.actions.put((action, payload))
    return sess


def _reap_locked() -> None:
    """Drop finished sessions older than IDLE_TIMEOUT (caller holds lock)."""
    now = time.time()
    dead = [s for s, sess in SESSIONS.items()
            if sess.status != "waiting" and sess.status != "starting"
            and now - sess.created_at > IDLE_TIMEOUT]
    for s in dead:
        SESSIONS.pop(s, None)


# ---------------------------------------------------------------------------
# Worker: owns the driver, streams screenshots, applies queued actions
# ---------------------------------------------------------------------------
def _worker(sess: LoginSession) -> None:
    driver = None
    try:
        driver = _make_driver(sess.profile_dir)
        driver.set_window_size(*WINDOW)
        driver.get("https://www.linkedin.com/login")
        deadline = time.time() + TOTAL_TIMEOUT
        with sess.lock:
            sess.status = "waiting"
            sess.message = ("Sign in on LinkedIn's page. Enter your details "
                            "below — the tool never stores your password.")
        while time.time() < deadline and not sess.stop.is_set():
            _drain(sess, driver)
            if sess.stop.is_set():
                break
            try:
                png = driver.get_screenshot_as_png()
                url = driver.current_url
            except Exception as exc:  # noqa: BLE001 — browser died
                with sess.lock:
                    sess.status = "failed"
                    sess.message = f"Browser session lost ({type(exc).__name__}). Try again."
                return
            with sess.lock:
                sess.screenshot_b64 = base64.b64encode(png).decode()
                sess.url = url
                try:
                    opts = detect_twofa(driver)
                except Exception:  # noqa: BLE001 — never break the stream
                    opts = {}
                sess.twofa_options = opts
                if sess.twofa_selected and sess.twofa_selected not in opts:
                    sess.twofa_selected = None   # page moved past that choice
            if _logged_in(url):
                with sess.lock:
                    sess.status = "success"
                    sess.message = ("Signed in! The session is saved for this "
                                    "profile — Run new scan now works unattended.")
                logger.info("LinkedIn web sign-in completed for profile %s", sess.slug)
                return
            if time.time() - sess.last_action_at > IDLE_TIMEOUT:
                with sess.lock:
                    sess.status = "cancelled"
                    sess.message = "Timed out waiting for you — nothing was saved."
                return
            time.sleep(POLL_SECONDS)
        with sess.lock:
            if sess.status == "waiting":
                sess.status = "cancelled" if sess.stop.is_set() else "failed"
                sess.message = ("Cancelled." if sess.stop.is_set()
                                else "Sign-in window expired — nothing was saved.")
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        logger.exception("LinkedIn login worker failed")
        with sess.lock:
            sess.status = "failed"
            sess.message = f"Could not open the sign-in browser: {exc}"
    finally:
        if driver is not None:
            try:
                driver.quit()   # flushes cookies to the profile dir
            except Exception:  # noqa: BLE001
                pass


def _logged_in(url: str) -> bool:
    from . import linkedin_scraper as ls
    return bool(url) and not ls.is_logged_out_url(url) and "linkedin.com" in (url or "")


def _drain(sess: LoginSession, driver) -> None:
    while True:
        try:
            action, payload = sess.actions.get_nowait()
        except queue.Empty:
            return
        try:
            _apply(action, payload, driver, sess)
        except Exception as exc:  # noqa: BLE001 — report, keep the session alive
            with sess.lock:
                sess.message = f"That didn't work ({type(exc).__name__}: {exc}). Try again."


def _apply(action: str, payload: dict, driver,
           sess: "LoginSession | None" = None) -> None:
    """Apply one user action to the live LinkedIn page.

    SECURITY: 'credentials' carries the password. Do not log it, echo it,
    or store it anywhere — it is typed into LinkedIn's form and dropped.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    if action == "cancel":
        return  # the stop event was already set by cancel()

    if action == "credentials":
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        if not email or not password:
            raise ValueError("Both email and password are required.")
        from . import linkedin_scraper as ls
        # LinkedIn's DOM has hidden duplicate inputs first — resolve visible.
        e = ls.find_visible(driver, ls.EMAIL_SELECTORS, "email")
        p = ls.find_visible(driver, ls.PASSWORD_SELECTORS, "password")
        ls.focus_type(driver, e, email)
        ls.focus_type(driver, p, password)
        # The password lives only in these locals; nothing else keeps it.
        del password
        ls.click_sign_in(driver)

    elif action == "text":
        text = payload.get("text") or ""
        if text:
            driver.switch_to.active_element.send_keys(text)

    elif action == "keys":
        key = {"Enter": Keys.ENTER, "Tab": Keys.TAB,
               "Backspace": Keys.BACKSPACE, "Escape": Keys.ESCAPE}.get(
                   payload.get("key", "Enter"))
        if key is None:
            raise ValueError("Unsupported key.")
        driver.switch_to.active_element.send_keys(key)

    elif action == "click":
        x, y = int(payload["x"]), int(payload["y"])
        # Absolute CDP click — no pointer-drift between ActionChains calls.
        for etype in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": etype, "x": x, "y": y,
                "button": "left", "clickCount": 1})

    elif action == "twofa":
        # Toggle a two-factor verification method on LinkedIn's checkpoint
        # page. method='' clears the selection (no click); a method key clicks
        # that option so LinkedIn shows its code entry / sends the code.
        method = (payload.get("method") or "").strip().lower()
        if sess is not None:
            with sess.lock:
                sess.twofa_selected = method or None
        if not method:
            return
        if method not in TWOFA_METHODS:
            raise ValueError("Unknown verification method.")
        el = _find_twofa_option(driver, TWOFA_METHODS[method])
        if el is None:
            raise ValueError(
                f"LinkedIn isn't offering “{TWOFA_METHODS[method]['label']}” "
                "on this page right now.")
        el.click()
        _click_continue(driver)
        if sess is not None:
            with sess.lock:
                sess.message = (
                    f"Chose {TWOFA_METHODS[method]['label']} — watch the "
                    "picture for the code prompt.")

    else:
        raise ValueError(f"Unknown action: {action}")
