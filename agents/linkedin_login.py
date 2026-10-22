"""One-time LinkedIn sign-in into the persistent scraping profile.

    uv run python -m agents.linkedin_login            # sign in
    uv run python -m agents.linkedin_login --status   # check session
    uv run python -m agents.linkedin_login --reset    # wipe the profile

How this keeps your password out of the tool
--------------------------------------------
A real Chrome window opens on LinkedIn's own sign-in page. You type your
password (or use a passkey / 2FA) directly into LinkedIn — this program
never reads, stores, or transmits it. LinkedIn then hands the browser
session cookies, which Chrome saves into the profile directory
(``cache/linkedin_profile`` by default, override with
``LINKEDIN_PROFILE_DIR``). Every later scrape reuses that profile, so the
web viewer's "Run new scan" button works with zero stored credentials.

On the VPS the same profile lives in a named volume; sign in once with::

    docker compose exec -T job_search xvfb-run -a \\
        python -m agents.linkedin_login

and the session persists across redeploys.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import linkedin_scraper as ls


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
    args = parser.parse_args(argv)

    profile = os.path.abspath(
        args.profile_dir or ls.profile_dir_from_env() or ls.DEFAULT_PROFILE_DIR)

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
