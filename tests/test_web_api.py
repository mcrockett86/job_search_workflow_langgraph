"""API tests for the viewer web app (DB layer only — no network, no LLM)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agents import db, web


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dbfile = os.path.join(tmp_path, "jobs.db")
    monkeypatch.setattr(db, "DB_PATH", dbfile)
    # Seed the DB before the app handles requests.
    conn = db.get_conn(dbfile)
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
