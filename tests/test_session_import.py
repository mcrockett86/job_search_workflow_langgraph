"""Tests for the LinkedIn session-import flow (agents/session_import.py +
the /api/login/* endpoints in agents/web.py).

No selenium, no network, no LinkedIn: install_session's driver is faked,
and the pairing lifecycle runs against the in-process registry.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from agents import profiles, session_import, user_config, web


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(profiles, "USER_DIR", os.path.join(root, "user_config"))
    monkeypatch.setattr(profiles, "PROFILES_FILE",
                        os.path.join(root, "user_config", "profiles.json"))
    monkeypatch.setattr(profiles, "PROFILES_DIR",
                        os.path.join(root, "user_config", "profiles"))
    monkeypatch.setattr(profiles, "CACHE_DIR", os.path.join(root, "cache"))
    monkeypatch.setattr(profiles, "PROFILE_CACHE_DIR",
                        os.path.join(root, "cache", "profiles"))
    monkeypatch.setattr(user_config, "_override_dir", None)
    monkeypatch.delenv("LINKEDIN_PROFILE_DIR", raising=False)
    token = profiles.set_current(None)
    profiles.ensure_init()
    if "jen2" not in (profiles._read_registry() or {}).get("profiles", {}):  # noqa: SLF001
        profiles.create("jen2")     # the profile from the bug report
    yield root
    profiles.reset_current(token)
    # pairing registry is module-global: leave it clean for the next test
    session_import._PAIRS.clear()          # noqa: SLF001
    session_import._PAIRS_BY_SLUG.clear()  # noqa: SLF001


class FakeDriver:
    """Records CDP cookie injections; /feed 'validity' is scripted."""

    valid = True   # class-level knob the tests flip before starting

    def __init__(self):
        self.set_cookies = []
        self.urls = []
        self.quit_called = False
        self.profile_dir: str | None = None

    @property
    def current_url(self):
        # scripted: a 'valid' session stays on /feed, an invalid one lands
        # on the authwall (what LinkedIn does with a dead li_at)
        return ("https://www.linkedin.com/feed/" if FakeDriver.valid
                else "https://www.linkedin.com/authwall?trk=gq")

    def set_window_size(self, w, h):
        pass

    def get(self, url):
        self.urls.append(url)

    def execute_cdp_cmd(self, cmd, params):
        assert cmd == "Network.setCookie"
        self.set_cookies.append(dict(params))

    def quit(self):
        # real Chrome flushes cookie data into the profile dir on quit;
        # mimic that side effect so session_state() sees a saved session
        if self.profile_dir:
            os.makedirs(self.profile_dir, exist_ok=True)
            with open(os.path.join(self.profile_dir, "Cookies"), "wb") as fh:
                fh.write(b"\x00fake-sqlite")
        self.quit_called = True


@pytest.fixture
def fake_driver(monkeypatch):
    from agents import linkedin_scraper as ls
    made: list[FakeDriver] = []

    def _setup(headless=None, profile_dir=None):
        d = FakeDriver()
        d.profile_dir = profile_dir
        made.append(d)
        return d

    FakeDriver.valid = True
    monkeypatch.setattr(ls, "_setup_driver", _setup)
    monkeypatch.setattr(session_import, "VERIFY_BUDGET", 0.2)  # fast tests
    return made


# ---------------------------------------------------------------------------
# pairing lifecycle
# ---------------------------------------------------------------------------
def test_pair_start_returns_usable_code():
    d = session_import.start_pair("jen2")
    assert len(d["code"]) == 8
    assert d["slug"] == "jen2"
    st = session_import.pair_status(d["code"])
    assert st["state"] == "pending"


def test_pair_codes_avoid_ambiguous_characters():
    for _ in range(20):
        code = session_import.start_pair("mike")["code"]
        assert not set(code) & set("0O1IL")


def test_new_pair_replaces_pending_one_for_same_slug():
    first = session_import.start_pair("jen2")["code"]
    second = session_import.start_pair("jen2")["code"]
    assert first != second
    assert session_import.pair_status(first) is None
    assert session_import.pair_status(second)["state"] == "pending"


def test_claim_requires_valid_li_at_and_does_not_burn_code_on_junk():
    code = session_import.start_pair("jen2")["code"]
    with pytest.raises(ValueError):
        session_import.claim_pair(code, {"nope": "x"})
    # code still pending after a junk upload — the user can fix and retry
    assert session_import.pair_status(code)["state"] == "pending"


def test_claim_consumes_code_on_success(fake_driver):
    code = session_import.start_pair("jen2")["code"]
    res = session_import.claim_pair(code, {"li_at": "AQE" + "x" * 60})
    assert res["ok"] is True
    assert session_import.pair_status(code)["state"] == "claimed"
    with pytest.raises(ValueError):
        session_import.claim_pair(code, {"li_at": "AQE" + "x" * 60})


def test_claim_rejects_unknown_or_expired_code():
    with pytest.raises(ValueError):
        session_import.claim_pair("NOSUCH00", {"li_at": "AQE" + "x" * 60})
    code = session_import.start_pair("jen2")["code"]
    session_import._PAIRS[code].created_at = time.time() - 10 ** 6  # noqa: SLF001
    with pytest.raises(ValueError):
        session_import.claim_pair(code, {"li_at": "AQE" + "x" * 60})


def test_claim_wipes_old_profile_then_injects_and_verifies(fake_driver):
    p = profiles.get("jen2")
    p.ensure_dirs()
    os.makedirs(p.linkedin_profile_dir, exist_ok=True)
    stale = os.path.join(p.linkedin_profile_dir, "stale_cookie")
    with open(stale, "w") as fh:
        fh.write("old-person-session")

    code = session_import.start_pair("jen2")["code"]
    res = session_import.claim_pair(
        code, {"li_at": "AQE" + "x" * 60, "lidc": "d=123"})
    assert res["ok"] is True

    d = fake_driver[0]
    names = {c["name"] for c in d.set_cookies}
    assert "li_at" in names and "lidc" in names
    assert all(c["domain"] == ".linkedin.com" for c in d.set_cookies)
    assert d.quit_called                      # cookies flushed to disk
    assert not os.path.exists(stale)          # old session really gone


def test_rejected_session_reports_failure_without_retry_loop(fake_driver):
    FakeDriver.valid = False
    code = session_import.start_pair("jen2")["code"]
    res = session_import.claim_pair(code, {"li_at": "AQE" + "x" * 60})
    assert res["ok"] is False
    assert "expired or revoked" in res["message"]


def test_late_authwall_redirect_is_not_a_false_positive(fake_driver, monkeypatch):
    """Regression: a dead cookie can sit on /feed for seconds before
    LinkedIn redirects to /authwall. The verifier must wait it out, not
    trust a single early snapshot (this false-positived in live testing)."""
    flips = {"n": 0}

    class LateWall(FakeDriver):
        @property
        def current_url(self):
            flips["n"] += 1
            return ("https://www.linkedin.com/feed/" if flips["n"] < 3
                    else "https://www.linkedin.com/authwall?trk=gq")

    from agents import linkedin_scraper as ls
    monkeypatch.setattr(ls, "_setup_driver",
                        lambda headless=None, profile_dir=None: LateWall())
    monkeypatch.setattr(session_import, "VERIFY_BUDGET", 0.3)
    code = session_import.start_pair("jen2")["code"]
    res = session_import.claim_pair(code, {"li_at": "AQE" + "x" * 60})
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# security: the session material never leaks into anything observable
# ---------------------------------------------------------------------------
def test_no_response_or_error_ever_contains_the_cookie(fake_driver):
    secret = "AQE" + "SECRETV" + "alue" * 8
    code = session_import.start_pair("jen2")["code"]
    res = session_import.claim_pair(code, {"li_at": secret})
    assert secret not in json.dumps(res)
    with pytest.raises(ValueError) as ei:
        session_import.claim_pair(code, {"li_at": secret})   # already claimed
    assert secret not in str(ei.value)


def test_paste_rejects_junk_without_echoing_it():
    with pytest.raises(ValueError) as ei:
        session_import.paste_session("jen2", "not a cookie at all!!")
    assert "not a cookie" not in str(ei.value)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(web.app)


def _hdr(slug):
    return {"X-Profile": slug}


def test_pair_start_endpoint_scoped_per_profile(client):
    a = client.post("/api/login/pair/start", headers=_hdr("jen2")).json()
    b = client.post("/api/login/pair/start", headers=_hdr("mike")).json()
    assert a["profile"] == "jen2" and b["profile"] == "mike"
    assert a["code"] != b["code"]


def test_claim_endpoint_flow_and_profile_pin(client, fake_driver):
    """The code pins the profile: an X-Profile header cannot redirect it."""
    code = client.post("/api/login/pair/start", headers=_hdr("jen2")).json()["code"]
    r = client.post("/api/login/pair/claim", headers=_hdr("mike"),
                    json={"code": code, "cookies": {"li_at": "AQE" + "x" * 60}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake_driver[0].profile_dir.endswith(os.path.join("linkedin_profile", "jen2"))
    st = client.post("/api/login/pair/check", json={"code": code}).json()
    assert st["state"] == "claimed" and st["result"]["ok"] is True


def test_claim_endpoint_bad_code_is_400_without_leak(client):
    r = client.post("/api/login/pair/claim",
                    json={"code": "BADCODE1", "cookies": {"li_at": "AQE" + "x" * 60}})
    assert r.status_code == 400
    assert "AQE" not in r.text


def test_paste_endpoint_wipes_and_reports(client, fake_driver):
    r = client.post("/api/login/paste", headers=_hdr("jen2"),
                    json={"li_at": "AQE" + "x" * 60})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "AQE" not in r.text


def test_session_endpoint_reflects_saved_state(client, fake_driver):
    assert client.get("/api/login/session", headers=_hdr("jen2")).json()["state"] == "none"
    client.post("/api/login/paste", headers=_hdr("jen2"),
                json={"li_at": "AQE" + "x" * 60})
    assert client.get("/api/login/session", headers=_hdr("jen2")).json()["state"] == "saved"


def test_reset_endpoint_forgets_session(client, fake_driver):
    client.post("/api/login/paste", headers=_hdr("jen2"),
                json={"li_at": "AQE" + "x" * 60})
    r = client.post("/api/login/reset", headers=_hdr("jen2"))
    assert r.status_code == 200
    assert client.get("/api/login/session", headers=_hdr("jen2")).json()["state"] == "none"


def test_legacy_relay_endpoints_are_gone(client):
    assert client.get("/api/login/status").status_code == 410
    assert client.post("/api/login/start").status_code == 410


def test_pair_check_unknown_code_is_404(client):
    assert client.post("/api/login/pair/check", json={"code": "NOPE1234"}).status_code == 404
