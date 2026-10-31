"""LinkedIn sign-in: locally into the scraping profile, or paired to the server.

    uv run python -m agents.linkedin_login              # sign in (this machine)
    uv run python -m agents.linkedin_login --status     # check session
    uv run python -m agents.linkedin_login --reset      # wipe the profile
    uv run python -m agents.linkedin_login --pair CODE --server https://… \
        --profile jen2                                  # sign in FOR the server

How this keeps your password out of the tool
--------------------------------------------
A real Chrome window opens on LinkedIn's own sign-in page. You type your
password (or use a passkey / 2FA) directly into LinkedIn — this program
never reads, stores, or transmits it. LinkedIn then hands the browser
session cookies, which Chrome saves into the profile directory
(``cache/linkedin_profile`` by default, override with
``LINKEDIN_PROFILE_DIR``). Every later scrape reuses that profile, so the
web viewer's "Run new scan" button works with zero stored credentials.

The ``--pair`` mode is how the VPS gets a session without any screen:
sign in in a real window HERE (2FA is just a normal browser login), and
the helper uploads only the resulting session cookies to the viewer over
HTTPS, authenticated by the single-use pairing code the viewer shows.
The cookies are password-equivalent: send them only to a server you
control, over its HTTPS origin. The throwaway Chrome profile is deleted
when the helper exits.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

from . import linkedin_scraper as ls

# The cookies worth importing; li_at is the session token that matters.
_IMPORT_NAMES = ("li_at", "JSESSIONID", "lidc", "bcookie", "lang", "fsb", "visit")


def _upload(server: str, code: str, cookies: dict, profile: str) -> dict:
    """POST the session to the viewer's pairing endpoint. Basic-auth comes
    from JOB_SEARCH_AUTH env ("user:password") when the site gates access —
    the same credentials the user's browser already sends to the viewer."""
    url = server.rstrip("/") + "/api/login/pair/claim"
    body = json.dumps({"code": code, "cookies": cookies}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    auth = os.environ.get("JOB_SEARCH_AUTH")
    if auth:
        import base64
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(auth.encode()).decode())
    if profile:
        req.add_header("X-Profile", profile)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _pair_login(code: str, server: str, profile: str, timeout: int) -> int:
    """Real visible Chrome, throwaway profile; on login, upload the cookies."""
    tmp = tempfile.mkdtemp(prefix="li-pair-")
    print("Opening a real Chrome window on LinkedIn's sign-in page.")
    print("Sign in there (2FA, push, passkey — anything). This window is")
    print("throwaway: it is deleted when you finish, and only the session")
    print(f"cookies go to {server} for profile “{profile or 'the paired one'}”.")
    driver = ls._setup_driver(headless=False, profile_dir=tmp)  # noqa: SLF001
    try:
        driver.get("https://www.linkedin.com/login")
        deadline = time.time() + timeout
        signed_in = False
        while time.time() < deadline:
            time.sleep(2)
            try:
                url = driver.current_url
            except Exception:  # noqa: BLE001 — window closed
                print("Browser closed before sign-in finished — nothing sent.")
                return 1
            if not ls.is_logged_out_url(url) and "linkedin.com" in url:
                signed_in = True
                break
        if not signed_in:
            print("Timed out waiting for sign-in — nothing was sent.")
            return 1
        # Give LinkedIn a beat to finish writing cookies post-redirect.
        time.sleep(2)
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()
                   if c["name"] in _IMPORT_NAMES and c.get("value")}
        if "li_at" not in cookies:
            print("Signed in, but no li_at session cookie was found — "
                  "nothing was sent.")
            return 1
        print(f"Signed in. Uploading session ({len(cookies)} cookies)…")
        try:
            res = _upload(server, code, cookies, profile)
        except Exception as exc:  # noqa: BLE001 — no cookie material in logs
            print(f"Upload failed ({type(exc).__name__}). The pairing code "
                  "may still be pending — rerun the same command within "
                  "15 minutes, or start a new sign-in in the viewer.")
            return 1
        if res.get("ok"):
            print(res.get("message", "Session imported."))
            return 0
        print(res.get("message", "The server rejected the session."))
        return 1
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agents.linkedin_login",
        description="Sign LinkedIn into the persistent scraping profile "
                    "(the tool never sees your password).",
    )
    parser.add_argument("--profile-dir", default=None,
                        help="Profile directory (default: $LINKEDIN_PROFILE_DIR "
                             "or cache/linkedin_profile)")
    parser.add_argument("--status", action="store_true",
                        help="Only check whether the saved session is still valid")
    parser.add_argument("--reset", action="store_true",
                        help="Delete the profile directory and exit")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Seconds to wait for the human sign-in (default 300)")
    parser.add_argument("--pair", default=None, metavar="CODE",
                        help="Pairing code from the viewer: sign in HERE and "
                             "upload the session to --server for the viewer's "
                             "selected profile")
    parser.add_argument("--server",
                        default=os.environ.get("JOB_SEARCH_URL",
                                               "https://job-search.blueskyengineering.net"),
                        help="Viewer origin (default: $JOB_SEARCH_URL or the "
                             "published site)")
    parser.add_argument("--profile", default="",
                        help="Viewer profile slug this session is for "
                             "(sent as X-Profile; the pairing code already "
                             "pins it server-side)")
    args = parser.parse_args(argv)

    if args.pair:
        return _pair_login(args.pair.strip().upper(), args.server,
                           args.profile, args.timeout)

    profile = os.path.abspath(
        args.profile_dir or ls.linkedin_profile_dir())

    if args.reset:
        shutil.rmtree(profile, ignore_errors=True)
        print(f"Profile removed: {profile}")
        return 0

    print(f"Profile: {profile}")
    driver = ls._setup_driver(headless=args.status, profile_dir=profile)  # noqa: SLF001
    try:
        if args.status:
            ok = ls.session_valid(driver)
            print("Session: " + ("VALID — scraping can run unattended"
                                 if ok else
                                 "NOT signed in — run `python -m agents.linkedin_login`"))
            return 0 if ok else 1
        if ls.interactive_login(driver, timeout=args.timeout):
            print("Signed in. The session is saved in the profile — the web "
                  "viewer's Run button can now scrape without any password.\n"
                  "Tip: `python -m agents.linkedin_login --status` checks it later.")
            return 0
        print("Timed out waiting for sign-in — nothing was saved.")
        return 1
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
