"""Tests for the headless-UA stealth that keeps LinkedIn's Cloudflare wall
from blocking fresh sign-in sessions (``Sorry, you have been blocked``)."""

from __future__ import annotations

import pytest

from agents import linkedin_scraper as ls


class FakeDriver:
    """Records CDP calls; serves a canned navigator.userAgent."""

    def __init__(self, ua: str) -> None:
        self._ua = ua
        self.cdp: list[tuple[str, dict]] = []

    def execute_script(self, _script: str) -> str:
        return self._ua

    def execute_cdp_cmd(self, method: str, params: dict) -> None:
        self.cdp.append((method, params))


HEADLESS_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36")
CLEAN_UA = HEADLESS_UA.replace("HeadlessChrome", "Chrome")


def test_headless_ua_is_replaced_on_both_surfaces():
    d = FakeDriver(HEADLESS_UA)
    ls._deheadless_ua(d)  # noqa: SLF001
    methods = [m for m, _ in d.cdp]
    assert methods == ["Network.setUserAgentOverride",
                       "Page.addScriptToEvaluateOnNewDocument"]
    header = d.cdp[0][1]["userAgent"]
    script = d.cdp[1][1]["source"]
    assert "HeadlessChrome" not in header
    assert "Chrome/156.0.0.0" in header          # real version preserved
    assert "HeadlessChrome" not in script        # JS surface fixed too
    assert "navigator" in script and "userAgent" in script


def test_headful_clean_ua_is_left_alone():
    d = FakeDriver(CLEAN_UA)
    ls._deheadless_ua(d)  # noqa: SLF001
    assert d.cdp == []


def test_unknown_ua_never_raises():
    d = FakeDriver(None)  # type: ignore[arg-type]
    ls._deheadless_ua(d)  # noqa: SLF001
    assert d.cdp == []


def test_setup_driver_applies_stealth_best_effort():
    """_setup_driver must call the stealth hook and swallow its failures."""
    import inspect
    src = inspect.getsource(ls._setup_driver)
    assert "_deheadless_ua" in src
    assert "except" in src  # never breaks driver creation
