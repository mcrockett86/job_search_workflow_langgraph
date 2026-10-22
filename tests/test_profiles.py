"""Tests for named profiles: registry, seeding, migration, and per-profile
isolation of resume / search URLs / discovered-job cache. No network, no LLM.
"""

from __future__ import annotations

import os

import pytest

from agents import db, profiles, user_config

try:
    from .test_user_config import build_pdf
except ImportError:  # tests dir on sys.path (no package __init__)
    from test_user_config import build_pdf


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fresh fake repo root: registry + profile dirs + caches under tmp."""
    root = str(tmp_path)
    monkeypatch.setattr(profiles, "USER_DIR", os.path.join(root, "user_config"))
    monkeypatch.setattr(profiles, "PROFILES_FILE",
                        os.path.join(root, "user_config", "profiles.json"))
    monkeypatch.setattr(profiles, "PROFILES_DIR",
                        os.path.join(root, "user_config", "profiles"))
    monkeypatch.setattr(profiles, "CACHE_DIR", os.path.join(root, "cache"))
    monkeypatch.setattr(profiles, "PROFILE_CACHE_DIR",
                        os.path.join(root, "cache", "profiles"))
    for name in ("LEGACY_DB", "LEGACY_PRIOR_CSV", "LEGACY_RESULTS_CSV",
                 "LEGACY_RESUME_PDF", "LEGACY_RESUME_META", "LEGACY_URLS_FILE"):
        monkeypatch.setattr(profiles, name, os.path.join(root, name.lower()))
    # user_config must resolve through the profile dirs, not the override hook
    monkeypatch.setattr(user_config, "_override_dir", None)
    # tests may set_current(); restore the neutral default afterwards
    token = profiles.set_current(None)
    yield root
    profiles.reset_current(token)


# ---------------------------------------------------------------------------
# Registry & seeding
# ---------------------------------------------------------------------------
def test_seeds_mike_and_jen(home):
    slugs = [p["slug"] for p in profiles.list_profiles()]
    assert slugs == ["jen", "mike"]
    assert profiles.default_slug() == "mike"


def test_ensure_init_is_idempotent(home):
    profiles.ensure_init()
    profiles.create("bob")
    profiles.ensure_init()
    slugs = [p["slug"] for p in profiles.list_profiles()]
    assert "bob" in slugs and slugs.count("mike") == 1


def test_create_and_get(home):
    p = profiles.create("Jen Chen")  # slugifies
    assert p.slug == "jen-chen"
    assert os.path.isdir(p.config_dir) and os.path.isdir(p.cache_dir)
    with pytest.raises(ValueError):
        profiles.create("!!!")
    with pytest.raises(ValueError):
        profiles.create("jen")  # already exists
    with pytest.raises(KeyError):
        profiles.get("nobody")


# ---------------------------------------------------------------------------
# Legacy migration: mike keeps the pre-profile settings
# ---------------------------------------------------------------------------
def test_legacy_files_migrate_into_mike(home):
    # plant pre-profile flat files
    for p, content in [
        (profiles.LEGACY_RESUME_PDF, b"%PDF-1.4 planted"),
        (profiles.LEGACY_RESUME_META, b"filename=old.pdf\npages=1\n"),
        (profiles.LEGACY_URLS_FILE, b"# c\nhttps://old.example/search\n"),
        (profiles.LEGACY_PRIOR_CSV, b"job_url\nhttp://old/job1\n"),
        (profiles.LEGACY_DB, b"sqlite placeholder"),
    ]:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(content)

    profiles.ensure_init()

    mike = profiles.get("mike")
    assert open(mike.resume_pdf, "rb").read().startswith(b"%PDF")
    assert "old.example" in open(mike.urls_file).read()
    assert os.path.isfile(mike.prior_csv)
    assert os.path.isfile(mike.db_path)
    # legacy locations are now empty (moved, not copied)
    assert not os.path.exists(profiles.LEGACY_RESUME_PDF)
    assert not os.path.exists(profiles.LEGACY_URLS_FILE)

    # mike's profile now reports the migrated config as current
    profiles.set_current("mike")
    assert user_config.urls_are_custom()
    assert user_config.get_search_urls() == ["https://old.example/search"]
    assert user_config.get_resume_pdf_path() == mike.resume_pdf

    # jen starts clean: bundled defaults, no history
    profiles.set_current("jen")
    assert not user_config.urls_are_custom()
    assert user_config.get_resume_pdf_path() is None


# ---------------------------------------------------------------------------
# Isolation: resume / URLs / job DB per profile
# ---------------------------------------------------------------------------
def test_resume_isolation(home):
    pdf = build_pdf("Mike Markov — platform engineer")
    profiles.set_current("mike")
    user_config.save_resume_pdf(pdf, "mike.pdf")
    assert user_config.resume_meta()["filename"] == "mike.pdf"

    profiles.set_current("jen")
    assert user_config.get_resume_pdf_path() is None  # jen: bundled default
    user_config.save_resume_pdf(build_pdf("Jen Jameson — data scientist"), "jen.pdf")

    profiles.set_current("mike")
    assert user_config.resume_meta()["filename"] == "mike.pdf"
    assert user_config.resume_meta()["path"].endswith(
        os.path.join("profiles", "mike", "resume.pdf"))


def test_url_isolation(home):
    profiles.set_current("mike")
    user_config.save_urls(["https://www.linkedin.com/jobs/search/?keywords=mike"])
    profiles.set_current("jen")
    assert not user_config.urls_are_custom()  # jen falls back to defaults
    user_config.save_urls(["https://www.linkedin.com/jobs/search/?keywords=jen"])
    assert user_config.get_search_urls() == [
        "https://www.linkedin.com/jobs/search/?keywords=jen"]
    profiles.set_current("mike")
    assert user_config.get_search_urls() == [
        "https://www.linkedin.com/jobs/search/?keywords=mike"]


def test_reset_is_scoped_to_profile(home):
    for slug in ("mike", "jen"):
        profiles.set_current(slug)
        user_config.save_urls([f"https://x.example/{slug}"])
    profiles.set_current("jen")
    user_config.reset("all")
    assert not user_config.urls_are_custom()
    profiles.set_current("mike")
    assert user_config.urls_are_custom()  # mike untouched


def test_job_db_isolation(home):
    profiles.ensure_init()
    mike, jen = profiles.get("mike"), profiles.get("jen")

    conn = db.get_conn(mike.db_path)
    db.upsert_seen(conn, ["http://job/mike-only"])
    conn.close()

    conn = db.get_conn(jen.db_path)
    n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    conn.close()
    assert n == 0  # jen never sees mike's discovered jobs

    # and the contextvar-routed default resolves per profile
    profiles.set_current("mike")
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
    conn.close()
    profiles.set_current("jen")
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
    conn.close()


def test_prior_csv_paths_are_distinct(home):
    mike, jen = profiles.get("mike"), profiles.get("jen")
    assert mike.prior_csv != jen.prior_csv
    assert mike.prior_csv.endswith(os.path.join("mike", "target_jobs_old.csv"))
