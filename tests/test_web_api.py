"""API tests for the viewer web app (DB layer only — no network, no LLM)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agents import db, profiles, web


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate ALL profile state (registry, config dirs, per-profile DBs)
    # under tmp_path so tests never touch the real repo dirs.
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
    # Seed the DEFAULT profile's DB before the app handles requests.
    conn = db.get_conn(profiles.get(profiles.default_slug()).db_path)
    db.upsert_results(conn, [
        {"job_url": "http://x/a", "is_match": True, "company_name": "Acme",
         "job_title": "Data Scientist", "job_type": "Full-time",
         "salary_min": 120000, "salary_max": 180000, "summary": "Great role."},
        {"job_url": "http://x/b", "is_match": False, "company_name": "Globex",
         "job_title": "Chef", "job_type": "Part-time", "summary": "Not a fit."},
    ])
    db.upsert_seen(conn, ["http://x/c"])
    db.set_preview(conn, "http://x/a", "full preview text here", "Great role.")
    conn.close()
    return TestClient(web.app)


def test_viewer_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Job Search Viewer" in r.text


def test_facets_and_counts(client):
    d = client.get("/api/facets").json()
    assert d["counts"]["match"] == 1
    assert d["counts"]["rejected"] == 1
    assert d["counts"]["seen"] == 1
    assert d["counts"]["total"] == 3
    assert "Acme" in d["facets"]["companies"]


def test_jobs_filtering(client):
    r = client.get("/api/jobs", params={"status": "match"}).json()
    assert [j["job_url"] for j in r["jobs"]] == ["http://x/a"]
    r = client.get("/api/jobs", params={"search": "chef"}).json()
    assert [j["job_url"] for j in r["jobs"]] == ["http://x/b"]
    r = client.get("/api/jobs", params={"min_salary": 150000}).json()
    assert [j["job_url"] for j in r["jobs"]] == ["http://x/a"]
    # list payload must not carry the big preview blob
    assert "preview" not in r["jobs"][0]


def test_preview_uses_cache_without_network(client):
    r = client.get("/api/preview", params={"url": "http://x/a"})
    assert r.status_code == 200
    d = r.json()
    assert d["cached"] is True
    assert d["preview"] == "full preview text here"


def test_preview_unknown_url(client):
    assert client.get("/api/preview", params={"url": "http://nope"}).status_code == 404


def test_export_csv_and_md(client):
    r = client.get("/api/export", params={"format": "csv", "status": "match"})
    assert r.status_code == 200
    assert "Data Scientist" in r.text
    assert "Great role." in r.text
    assert "Chef" not in r.text
    assert "attachment" in r.headers["content-disposition"]
    r = client.get("/api/export", params={"format": "md"})
    assert "## Data Scientist — Acme" in r.text
    assert "link: http://x/a" in r.text


def test_run_requires_credentials(client, monkeypatch):
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    monkeypatch.delenv("LINKEDIN_PROFILE_DIR", raising=False)
    r = client.post("/api/run")
    assert r.status_code == 400
    assert "LINKEDIN_EMAIL" in r.json()["detail"]
    assert "linkedin_login" in r.json()["detail"]  # points at passwordless path


def test_run_accepts_persistent_profile_without_password(client, monkeypatch):
    """A signed-in profile dir satisfies the auth gate — no password env vars."""
    from agents import web as web_mod
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    monkeypatch.setenv("LINKEDIN_PROFILE_DIR", "/tmp/js_test_profile")
    monkeypatch.setattr("agents.web._run_pipeline_in_thread", lambda: None)
    r = client.post("/api/run")
    assert r.status_code == 200
    web_mod.run_state.running = False  # don't leak the lock into other tests


def test_run_status_shape(client):
    d = client.get("/api/run/status").json()
    assert set(d) >= {"running", "log", "error"}
    assert d["running"] is False


# ---------------------------------------------------------------------------
# Profiles: API scoping
# ---------------------------------------------------------------------------
def test_profiles_listed_with_mike_default(client):
    d = client.get("/api/profiles").json()
    slugs = [p["slug"] for p in d["profiles"]]
    assert "mike" in slugs and "jen" in slugs
    assert d["default"] == "mike"


def test_config_reports_active_profile(client):
    assert client.get("/api/config").json()["profile"] == "mike"
    assert client.get("/api/config?profile=jen").json()["profile"] == "jen"


def test_jobs_are_scoped_per_profile(client):
    # seeded rows live in mike's DB; jen starts empty
    assert len(client.get("/api/jobs").json()["jobs"]) == 3
    assert client.get("/api/jobs?profile=jen").json()["jobs"] == []
    assert client.get("/api/facets?profile=jen").json()["counts"].get("total", 0) == 0


def test_resume_upload_scopes_to_profile(client):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from test_user_config import build_pdf
    pdf = build_pdf("Jen Jameson — data scientist")
    r = client.post("/api/config/resume?profile=jen",
                    files={"file": ("jen.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    assert client.get("/api/config?profile=jen").json()["resume"]["custom"] is True
    assert client.get("/api/config?profile=mike").json()["resume"]["custom"] is False


def test_unknown_profile_rejected(client):
    assert client.get("/api/jobs?profile=ghost").status_code == 404


def test_create_profile_via_api(client):
    r = client.post("/api/profiles", json={"name": "Sam Lee"})
    assert r.status_code == 200 and r.json()["slug"] == "sam-lee"
    assert client.get("/api/jobs?profile=sam-lee").json()["jobs"] == []
    # duplicate -> 400
    assert client.post("/api/profiles", json={"name": "sam lee"}).status_code == 400
