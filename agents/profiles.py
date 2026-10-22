"""Named search profiles: each profile keeps its own resume, search URLs,
discovered-jobs database, and prior-jobs cache (so "seen" jobs never leak
between profiles).

Layout
------
user_config/profiles.json          registry: {profiles: {slug: {...}}, default}
user_config/profiles/<slug>/       resume.pdf, resume_meta.txt, search_urls.txt
cache/profiles/<slug>/             jobs.db, target_jobs_old.csv, results_output.csv

``ensure_init()`` seeds the registry (mike + jen) and MIGRATES the legacy
flat files (cache/jobs.db, cache/target_jobs_old.csv, results_output.csv,
user_config/resume.pdf, user_config/search_urls.txt) into mike's profile,
so the original owner's settings and history carry over untouched.

The "current profile" is a contextvar: HTTP requests set it from the
?profile= parameter; background runs set it at thread start. Code that
needs paths calls ``current()`` — nothing hardcodes a profile.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import time
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_DIR = os.path.join(ROOT, "user_config")
PROFILES_FILE = os.path.join(USER_DIR, "profiles.json")
PROFILES_DIR = os.path.join(USER_DIR, "profiles")
CACHE_DIR = os.path.join(ROOT, "cache")
PROFILE_CACHE_DIR = os.path.join(CACHE_DIR, "profiles")

# Legacy flat locations (pre-profiles) — migration sources, kept as defaults
# so the app still works if the registry was never initialized.
LEGACY_DB = os.path.join(CACHE_DIR, "jobs.db")
LEGACY_PRIOR_CSV = os.path.join(CACHE_DIR, "target_jobs_old.csv")
LEGACY_RESULTS_CSV = os.path.join(ROOT, "results_output.csv")
LEGACY_RESUME_PDF = os.path.join(USER_DIR, "resume.pdf")
LEGACY_RESUME_META = os.path.join(USER_DIR, "resume_meta.txt")
LEGACY_URLS_FILE = os.path.join(USER_DIR, "search_urls.txt")

DEFAULT_PROFILE = "mike"
SEED_PROFILES = ("mike", "jen")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def slugify(name: str) -> str:
    """'Jen Chen' -> 'jen-chen'. Raises ValueError when nothing survives."""
    slug = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
    slug = re.sub(r"[^a-z0-9-]", "", slug).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:32]
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(
            f"Invalid profile name {name!r} — use letters/numbers "
            "(e.g. 'jen', 'jen-chen').")
    return slug


@dataclass
class Profile:
    slug: str
    created_at: str = ""

    @property
    def config_dir(self) -> str:
        return os.path.join(PROFILES_DIR, self.slug)

    @property
    def cache_dir(self) -> str:
        return os.path.join(PROFILE_CACHE_DIR, self.slug)

    @property
    def resume_pdf(self) -> str:
        return os.path.join(self.config_dir, "resume.pdf")

    @property
    def resume_meta(self) -> str:
        return os.path.join(self.config_dir, "resume_meta.txt")

    @property
    def urls_file(self) -> str:
        return os.path.join(self.config_dir, "search_urls.txt")

    @property
    def db_path(self) -> str:
        return os.path.join(self.cache_dir, "jobs.db")

    @property
    def prior_csv(self) -> str:
        return os.path.join(self.cache_dir, "target_jobs_old.csv")

    @property
    def results_csv(self) -> str:
        return os.path.join(self.cache_dir, "results_output.csv")

    @property
    def linkedin_profile_dir(self) -> str:
        """Persistent Chrome profile holding THIS profile's LinkedIn session.

        Base dir = $LINKEDIN_PROFILE_DIR (VPS: inside the cache volume) or
        the repo cache dir; each search profile gets its own subdirectory so
        mike's LinkedIn login never leaks into jen's scans (and vice versa).
        """
        base = os.environ.get("LINKEDIN_PROFILE_DIR") or os.path.join(
            CACHE_DIR, "linkedin_profile")
        return os.path.join(base, self.slug)

    def ensure_dirs(self) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def _read_registry() -> dict | None:
    if not os.path.isfile(PROFILES_FILE):
        return None
    try:
        with open(PROFILES_FILE, encoding="utf-8") as fh:
            reg = json.load(fh)
        if isinstance(reg, dict) and isinstance(reg.get("profiles"), dict) and reg["profiles"]:
            return reg
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_registry(reg: dict) -> None:
    os.makedirs(USER_DIR, exist_ok=True)
    tmp = PROFILES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2, sort_keys=True)
    os.replace(tmp, PROFILES_FILE)


def _migrate_legacy_into(slug: str) -> list[str]:
    """Move the pre-profile flat files into the seed owner's profile dirs.

    One-time, idempotent: each file moves only when the legacy source exists
    and the destination does not. Returns what was moved (for logging).
    """
    p = Profile(slug)
    p.ensure_dirs()
    moved = []
    for src, dst in (
        (LEGACY_DB, p.db_path),
        (LEGACY_PRIOR_CSV, p.prior_csv),
        (LEGACY_RESULTS_CSV, p.results_csv),
        (LEGACY_RESUME_PDF, p.resume_pdf),
        (LEGACY_RESUME_META, p.resume_meta),
        (LEGACY_URLS_FILE, p.urls_file),
    ):
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.move(src, dst)
            moved.append(os.path.basename(src))
    return moved


def ensure_init() -> dict:
    """Create the registry (mike + jen) on first use; migrate legacy data
    into mike so his existing settings/history carry over. Idempotent."""
    reg = _read_registry()
    if reg:
        return reg
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    reg = {"default": DEFAULT_PROFILE,
           "profiles": {s: {"created_at": stamp} for s in SEED_PROFILES}}
    moved = _migrate_legacy_into(DEFAULT_PROFILE)
    if moved:
        reg["migrated_into_default"] = moved
    _write_registry(reg)
    for slug in SEED_PROFILES:
        Profile(slug).ensure_dirs()
    return reg


def list_profiles() -> list[dict]:
    reg = _read_registry() or ensure_init()
    out = []
    for slug, meta in sorted(reg["profiles"].items()):
        p = Profile(slug)
        out.append({
            "slug": slug,
            "created_at": meta.get("created_at", ""),
            "is_default": slug == reg.get("default"),
            "has_resume": os.path.isfile(p.resume_pdf),
            "has_custom_urls": os.path.isfile(p.urls_file),
            "has_history": os.path.isfile(p.db_path) or os.path.isfile(p.prior_csv),
        })
    return out


def default_slug() -> str:
    reg = _read_registry() or ensure_init()
    return reg.get("default", DEFAULT_PROFILE)


def get(slug: str) -> Profile:
    """Resolve a profile; raises KeyError for unknown slugs."""
    reg = _read_registry() or ensure_init()
    if slug not in reg["profiles"]:
        raise KeyError(slug)
    return Profile(slug, reg["profiles"][slug].get("created_at", ""))


def create(name: str) -> Profile:
    reg = _read_registry() or ensure_init()
    slug = slugify(name)
    if slug in reg["profiles"]:
        raise ValueError(f"Profile '{slug}' already exists.")
    reg["profiles"][slug] = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_registry(reg)
    p = Profile(slug)
    p.ensure_dirs()
    return p


# ---------------------------------------------------------------------------
# Current profile (contextvar — per request / per run thread)
# ---------------------------------------------------------------------------
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jobsearch_profile", default=None)


def current_slug() -> str:
    slug = _current.get()
    if slug:
        return slug
    try:
        return default_slug()
    except Exception:  # noqa: BLE001 — registry unreadable: legacy fallback
        return DEFAULT_PROFILE


def current() -> Profile:
    slug = current_slug()
    try:
        return get(slug)
    except KeyError:
        # Registry missing/unknown: behave like the pre-profiles app.
        return Profile(slug)


def set_current(slug: str | None):
    return _current.set(slug)


def reset_current(token) -> None:
    _current.reset(token)
