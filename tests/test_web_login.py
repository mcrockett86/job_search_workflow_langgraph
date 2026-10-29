"""Tests for the browser-relayed LinkedIn sign-in (agents/web_login.py).

A fake WebDriver stands in for Chrome: no selenium, no network, no LinkedIn.
Covers the worker loop, the action queue, per-profile session isolation, and
the HTTP endpoints (start/status/credentials/cancel) through TestClient.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from agents import profiles, web, web_login


class FakeElement:
    def __init__(self, visible=True, text="", attrs=None):
        self.typed = []
        self.cleared = 0
        self.clicked = 0
        self._visible = visible
        self.text = text
        self.attrs = attrs or {}

    def clear(self):
        self.cleared += 1

    def send_keys(self, k):
        self.typed.append(k)

    def click(self):
        self.clicked += 1

    def is_displayed(self):
        return self._visible

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.attrs.get(name, "")


class FakeDriver:
    """Minimal WebDriver double: records everything the worker does.

    Mirrors LinkedIn's trap: the generic 'input[type=email]' selector finds a
    HIDDEN duplicate first — only the #username/#password ids are visible.
    """

    def __init__(self, profile_dir):
        self.profile_dir = profile_dir
        self.url = "https://www.linkedin.com/login"
        self.elements: dict[tuple, list[FakeElement]] = {}
        self.cdp_events = []
        self.quit_called = False
        self.window = None
        self.active = FakeElement()
        # the real, visible login form
        self.email_visible = FakeElement()
        self.password_visible = FakeElement()
        self.signin_visible = FakeElement()
        # hidden decoys that come first in DOM order
        self.email_hidden = FakeElement(visible=False)
        self.password_hidden = FakeElement(visible=False)
        # LinkedIn checkpoint page with two verification options + Next
        self.totp_option = FakeElement()          # authenticator app (by id)
        self.email_option = FakeElement(text="Send to my email instead")
        self.next_button = FakeElement(text="Next")
        # visible body text of the current page (detect_code_sent reads it)
        self.body_text = ""

    # navigation / state -----------------------------------------------------
    def set_window_size(self, w, h):
        self.window = (w, h)

    def get(self, url):
        self.url = url

    @property
    def current_url(self):
        return self.url

    def get_screenshot_as_png(self):
        return b"\x89PNG-fake"

    def quit(self):
        self.quit_called = True

    def execute_script(self, script, *args):
        return None

    # element access -----------------------------------------------------------
    def find_elements(self, by, value):
        if value in ("input[type='email']", "input[autocomplete='username']"):
            return [self.email_hidden, self.email_visible]
        if value == "input#username":
            return [self.email_visible]
        if value in ("input[type='password']", "input[autocomplete='current-password']"):
            return [self.password_hidden, self.password_visible]
        if value in ("input#password", "input[name='session_password']"):
            return [self.password_visible]
        if "Sign in" in str(value) or value == "button[type='submit']":
            return [self.signin_visible]
        # checkpoint page: options exist only once LinkedIn redirected there
        if "checkpoint" in self.url:
            if value in ("#totp-auth", "#app-to-phone-totp-auth",
                         "#authenticator-app-auth", "#totp-auth-label"):
                return [self.totp_option]
            if value == web_login._SCAN_SELECTOR:
                return [self.email_option, self.next_button]
        return []

    def find_element(self, by, value):
        if value == "body":
            return FakeElement(text=self.body_text)
        els = self.find_elements(by, value)
        return els[0] if els else FakeElement()

    @property
    def switch_to(self):
        return _Switch(self)

    # CDP ----------------------------------------------------------------------
    def execute_cdp_cmd(self, cmd, params):
        self.cdp_events.append((cmd, params))


class _Switch:
    def __init__(self, driver):
        self._d = driver

    @property
    def active_element(self):
        return self._d.active


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated profile registry + fake driver factory + fast poll interval."""
    root = str(tmp_path)
    monkeypatch.setattr(profiles, "USER_DIR", os.path.join(root, "user_config"))
    monkeypatch.setattr(profiles, "PROFILES_FILE",
                        os.path.join(root, "user_config", "profiles.json"))
    monkeypatch.setattr(profiles, "PROFILES_DIR",
                        os.path.join(root, "user_config", "profiles"))
    monkeypatch.setattr(profiles, "CACHE_DIR", os.path.join(root, "cache"))
    monkeypatch.setattr(profiles, "PROFILE_CACHE_DIR",
                        os.path.join(root, "cache", "profiles"))
    profiles.ensure_init()

    made: list[FakeDriver] = []

    def factory(profile_dir):
        d = FakeDriver(profile_dir)
        made.append(d)
        return d

    monkeypatch.setattr(web_login, "_make_driver", factory)
    monkeypatch.setattr(web_login, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(web_login, "IDLE_TIMEOUT", 30)
    # no stale sessions from other tests
    web_login.SESSIONS.clear()
    yield {"root": root, "drivers": made}
    for sess in list(web_login.SESSIONS.values()):
        sess.stop.set()
    web_login.SESSIONS.clear()


def _wait_status(slug, wanted, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sess = web_login.get(slug)
        if sess and sess.snapshot()["status"] == wanted:
            return sess
        time.sleep(0.05)
    raise AssertionError(f"session {slug} never reached {wanted}: "
                         f"{sess.snapshot() if sess else 'no session'}")


# ---------------------------------------------------------------------------
# worker loop
# ---------------------------------------------------------------------------
def test_start_opens_login_page_and_streams(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    assert d.url == "https://www.linkedin.com/login"
    snap = sess.snapshot()
    assert snap["screenshot"]  # base64 png streamed to the client
    assert snap["url_host"] == "www.linkedin.com"


def test_credentials_action_fills_visible_fields_and_submits(env):
    """Regression: LinkedIn's DOM has hidden duplicate inputs first — the
    relay must type into the VISIBLE ones (this was ElementNotInteractable
    on the live VPS) and click the visible Sign in button."""
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    web_login.enqueue("mike", "credentials",
                      {"email": "a@b.c", "password": "hunter2"})
    deadline = time.time() + 5
    while time.time() < deadline and not d.signin_visible.clicked:
        time.sleep(0.05)
    assert d.email_visible.typed == ["a@b.c"]
    assert d.password_visible.typed == ["hunter2"]
    assert d.email_hidden.typed == []      # decoy untouched
    assert d.password_hidden.typed == []   # decoy untouched
    assert d.signin_visible.clicked >= 1   # Sign in clicked


def test_success_when_url_leaves_login(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/feed/"
    _wait_status("mike", "success")
    assert d.quit_called  # cookies flushed to the profile dir
    assert sess.snapshot()["status"] == "success"


def test_checkpoint_url_is_not_success(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    time.sleep(0.3)
    assert sess.snapshot()["status"] == "waiting"  # challenge != logged in


def test_cancel_stops_and_reports(env):
    web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    assert web_login.cancel("mike") is True
    _wait_status("mike", "cancelled")
    assert env["drivers"][0].quit_called


def test_click_uses_cdp_absolute_coords(env):
    web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    web_login.enqueue("mike", "click", {"x": 640, "y": 450})
    d = env["drivers"][0]
    deadline = time.time() + 5
    while time.time() < deadline and len(d.cdp_events) < 2:
        time.sleep(0.05)
    kinds = [p["type"] for _, p in d.cdp_events]
    assert kinds == ["mousePressed", "mouseReleased"]
    assert d.cdp_events[0][1]["x"] == 640 and d.cdp_events[0][1]["y"] == 450


def test_text_and_keys_actions(env):
    web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    web_login.enqueue("mike", "text", {"text": "123456"})
    web_login.enqueue("mike", "keys", {"key": "Enter"})
    d = env["drivers"][0]
    deadline = time.time() + 5
    while time.time() < deadline and len(d.active.typed) < 2:
        time.sleep(0.05)
    assert d.active.typed[0] == "123456"
    assert d.active.typed[1] == "\ue007"  # Keys.ENTER


def test_bad_action_reports_but_keeps_session(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    web_login.enqueue("mike", "credentials", {"email": "a@b.c", "password": ""})
    deadline = time.time() + 5
    while time.time() < deadline and "required" not in sess.snapshot()["message"]:
        time.sleep(0.05)
    assert sess.snapshot()["status"] == "waiting"  # still alive for a retry


# ---------------------------------------------------------------------------
# two-factor method toggle
# ---------------------------------------------------------------------------
def test_no_twofa_options_on_plain_login_page(env):
    """The plain login form's 'Email or phone' label must NOT read as a
    verification option — detection is gated to checkpoint pages."""
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    time.sleep(0.2)
    snap = sess.snapshot()
    assert snap["twofa_options"] == {}
    assert snap["twofa_selected"] is None


def test_twofa_options_detected_on_checkpoint_page(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    snap = sess.snapshot()
    deadline = time.time() + 5
    while time.time() < deadline:
        snap = sess.snapshot()
        if snap["twofa_options"]:
            break
        time.sleep(0.05)
    assert snap["twofa_options"] == {
        "app": "Authenticator app", "email": "Email"}


def test_twofa_select_clicks_option_and_continue(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    _wait_status("mike", "waiting")
    web_login.enqueue("mike", "twofa", {"method": "app"})
    deadline = time.time() + 5
    while time.time() < deadline and not d.totp_option.clicked:
        time.sleep(0.05)
    assert d.totp_option.clicked >= 1
    assert d.next_button.clicked >= 1          # Next pressed after the pick
    snap = sess.snapshot()
    assert snap["twofa_selected"] == "app"
    assert "Authenticator app" in snap["message"]


def test_twofa_toggle_off_clears_selection(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    web_login.enqueue("mike", "twofa", {"method": "app"})
    deadline = time.time() + 5
    while time.time() < deadline and sess.snapshot()["twofa_selected"] != "app":
        time.sleep(0.05)
    web_login.enqueue("mike", "twofa", {"method": ""})
    deadline = time.time() + 5
    while time.time() < deadline and sess.snapshot()["twofa_selected"] is not None:
        time.sleep(0.05)
    assert sess.snapshot()["twofa_selected"] is None


def test_twofa_unavailable_method_reports_keeps_session(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    # push is not on the fake checkpoint page
    web_login.enqueue("mike", "twofa", {"method": "push"})
    deadline = time.time() + 5
    while time.time() < deadline and "isn't offering" not in sess.snapshot()["message"]:
        time.sleep(0.05)
    assert sess.snapshot()["status"] == "waiting"   # still alive for a retry


def test_twofa_selected_clears_when_page_moves_on(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    web_login.enqueue("mike", "twofa", {"method": "app"})
    deadline = time.time() + 5
    while time.time() < deadline and sess.snapshot()["twofa_selected"] != "app":
        time.sleep(0.05)
    d.url = "https://www.linkedin.com/uas/request-password-reset"  # left page
    deadline = time.time() + 5
    while time.time() < deadline and sess.snapshot()["twofa_selected"] is not None:
        time.sleep(0.05)
    assert sess.snapshot()["twofa_selected"] is None


# ---------------------------------------------------------------------------
# 'code sent' notice
# ---------------------------------------------------------------------------
def test_code_sent_detected_with_masked_destination(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    d.body_text = ("Enter the code we sent to (***) ***-1234. "
                   "It may take a minute to arrive.")
    snap = sess.snapshot()
    deadline = time.time() + 5
    while time.time() < deadline:
        snap = sess.snapshot()
        if snap["code_sent"]:
            break
        time.sleep(0.05)
    assert snap["code_sent"] is True
    assert "(***) ***-1234" in snap["code_notice"]


def test_code_sent_generic_notice_without_destination(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    d.body_text = "Check your messages and enter the code."
    snap = sess.snapshot()
    deadline = time.time() + 5
    while time.time() < deadline:
        snap = sess.snapshot()
        if snap["code_sent"]:
            break
        time.sleep(0.05)
    assert snap["code_sent"] is True
    assert "verification code was sent" in snap["code_notice"]


def test_code_sent_not_detected_on_plain_login(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.body_text = "Enter the code we sent to (***) ***-1234"  # even if present
    time.sleep(0.2)
    snap = sess.snapshot()
    assert snap["code_sent"] is False          # gated to checkpoint pages
    assert snap["code_notice"] is None


def test_code_sent_latches_and_rearms_on_new_pick(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    d.body_text = "We sent a code to jo**@example.com"
    deadline = time.time() + 5
    while time.time() < deadline and not sess.snapshot()["code_sent"]:
        time.sleep(0.05)
    assert sess.snapshot()["code_sent"] is True
    # page moves past the confirmation: the latch keeps the notice visible
    d.body_text = "Enter the 6-digit code"
    d.body_text = ""
    time.sleep(0.2)
    assert sess.snapshot()["code_sent"] is True
    # choosing another method sends a fresh code: re-arm to 'not yet sent'
    web_login.enqueue("mike", "twofa", {"method": "app"})
    deadline = time.time() + 5
    while time.time() < deadline and sess.snapshot()["code_sent"]:
        time.sleep(0.05)
    assert sess.snapshot()["code_sent"] is False


def test_code_sent_never_leaks_unmasked_destination(env):
    """A page that prints a FULL phone/email must not echo it into the
    notice — only LinkedIn's own masked strings are ever shown."""
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    d.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    d.body_text = "We sent a code. Delivery target: 555-867-5309"
    snap = sess.snapshot()
    deadline = time.time() + 5
    while time.time() < deadline:
        snap = sess.snapshot()
        if snap["code_sent"]:
            break
        time.sleep(0.05)
    assert snap["code_sent"] is True
    assert "555-867-5309" not in snap["code_notice"]


def test_sessions_are_per_profile(env):
    web_login.start("mike", "/tmp/x/mike")
    web_login.start("jen", "/tmp/x/jen")
    _wait_status("mike", "waiting")
    _wait_status("jen", "waiting")
    assert len(env["drivers"]) == 2
    assert {d.profile_dir for d in env["drivers"]} == {"/tmp/x/mike", "/tmp/x/jen"}
    # cancelling mike leaves jen alone
    web_login.cancel("mike")
    _wait_status("mike", "cancelled")
    assert web_login.get("jen").snapshot()["status"] == "waiting"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(env):
    return TestClient(web.app)


def test_login_endpoints_flow(client, env):
    assert client.get("/api/login/status?profile=mike").json()["active"] is False

    r = client.post("/api/login/start?profile=mike")
    assert r.status_code == 200 and r.json()["profile"] == "mike"

    deadline = time.time() + 5
    while time.time() < deadline:
        d = client.get("/api/login/status?profile=mike").json()
        if d["active"] and d["screenshot"]:
            break
        time.sleep(0.05)
    assert d["status"] == "waiting"
    assert d["url_host"] == "www.linkedin.com"

    r = client.post("/api/login/credentials?profile=mike",
                    json={"email": "a@b.c", "password": "hunter2"})
    assert r.status_code == 200

    # the password must not appear anywhere in the status payload
    snap = client.get("/api/login/status?profile=mike").text
    assert "hunter2" not in snap

    r = client.post("/api/login/cancel?profile=mike")
    assert r.json()["ok"] is True


def test_login_actions_without_session_are_409(client):
    r = client.post("/api/login/credentials?profile=jen",
                    json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 409
    r = client.post("/api/login/twofa?profile=jen", json={"method": "app"})
    assert r.status_code == 409


def test_twofa_endpoint_flow(client, env):
    client.post("/api/login/start?profile=mike")
    _wait_status("mike", "waiting")
    env["drivers"][0].url = "https://www.linkedin.com/checkpoint/challenge/x"
    d: dict = {}
    deadline = time.time() + 5
    while time.time() < deadline:
        d = client.get("/api/login/status?profile=mike").json()
        if d["twofa_options"]:
            break
        time.sleep(0.05)
    assert d["twofa_options"]["app"] == "Authenticator app"

    r = client.post("/api/login/twofa?profile=mike", json={"method": "app"})
    assert r.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        d = client.get("/api/login/status?profile=mike").json()
        if d["twofa_selected"] == "app":
            break
        time.sleep(0.05)
    assert d["twofa_selected"] == "app"

    # toggle off
    r = client.post("/api/login/twofa?profile=mike", json={"method": ""})
    assert r.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        d = client.get("/api/login/status?profile=mike").json()
        if d["twofa_selected"] is None:
            break
        time.sleep(0.05)
    assert d["twofa_selected"] is None


def test_login_status_is_scoped_per_profile(client, env):
    client.post("/api/login/start?profile=mike")
    _wait_status("mike", "waiting")
    assert client.get("/api/login/status?profile=mike").json()["active"] is True
    assert client.get("/api/login/status?profile=jen").json()["active"] is False


def test_start_uses_profile_scoped_chrome_dir(client, env):
    client.post("/api/login/start?profile=jen")
    _wait_status("jen", "waiting")
    p = profiles.get("jen")
    assert env["drivers"][-1].profile_dir == p.linkedin_profile_dir
    assert p.linkedin_profile_dir.endswith(os.path.join("linkedin_profile", "jen"))
