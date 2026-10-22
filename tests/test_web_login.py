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
    def __init__(self):
        self.typed = []
        self.cleared = 0

    def clear(self):
        self.cleared += 1

    def send_keys(self, k):
        self.typed.append(k)


class FakeDriver:
    """Minimal WebDriver double: records everything the worker does."""

    def __init__(self, profile_dir):
        self.profile_dir = profile_dir
        self.url = "https://www.linkedin.com/login"
        self.elements: dict[str, FakeElement] = {}
        self.clicked_xpaths = []
        self.cdp_events = []
        self.quit_called = False
        self.window = None
        self.active = FakeElement()

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

    # element access -----------------------------------------------------------
    def find_element(self, by, value):
        el = self.elements.setdefault(value, FakeElement())
        if "Sign in" in value:
            self.clicked_xpaths.append(value)
        return el

    def switch_to_property(self):
        return self.active

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


def test_credentials_action_types_and_submits(env):
    sess = web_login.start("mike", "/tmp/x/mike")
    _wait_status("mike", "waiting")
    d = env["drivers"][0]
    web_login.enqueue("mike", "credentials",
                      {"email": "a@b.c", "password": "hunter2"})
    deadline = time.time() + 5
    while time.time() < deadline and not d.clicked_xpaths:
        time.sleep(0.05)
    email_el = d.elements["input[type='email']"]
    pass_el = d.elements["input[type='password']"]
    assert email_el.typed == ["a@b.c"]
    assert pass_el.typed == ["hunter2"]
    assert d.clicked_xpaths  # Sign in button clicked


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
