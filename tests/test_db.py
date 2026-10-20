"""Tests for the SQLite job database layer."""

from __future__ import annotations

import os

import pytest

from agents import db


@pytest.fixture()
def conn(tmp_path):
    path = os.path.join(tmp_path, "jobs.db")
    c = db.get_conn(path)
    yield c
    c.close()


def test_schema_created(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in tables


def test_upsert_seen_inserts_and_bumps_run_count(conn):
    db.upsert_seen(conn, ["http://x/1", "http://x/2"])
    rows = db.list_jobs(conn)
    assert len(rows) == 2
    assert all(r["status"] == "seen" for r in rows)
    db.upsert_seen(conn, ["http://x/1"])
    r1 = next(r for r in db.list_jobs(conn) if r["job_url"] == "http://x/1")
    assert r1["run_count"] == 2


def test_upsert_results_sets_status_and_coalesces(conn):
    db.upsert_seen(conn, ["http://x/1"])
    db.upsert_results(conn, [{
        "job_url": "http://x/1", "is_match": True, "company_name": "Acme",
        "job_title": "ML Engineer", "job_type": "Full-time",
        "salary_min": 150000, "salary_max": 190000, "summary": "Great role.",
    }])
    row = db.list_jobs(conn)[0]
    assert row["status"] == "match"
    assert row["is_match"] == 1
    assert row["company_name"] == "Acme"
    assert row["salary_max"] == 190000
    # Later result with missing fields must not wipe earlier metadata.
    db.upsert_results(conn, [{"job_url": "http://x/1", "is_match": False}])
    row = db.list_jobs(conn)[0]
    assert row["status"] == "rejected"
    assert row["company_name"] == "Acme"


def test_nan_values_become_null(conn):
    import math

    db.upsert_results(conn, [{"job_url": "http://x/2", "is_match": False,
                              "company_name": float("nan"), "salary_min": math.nan}])
    row = db.list_jobs(conn)[0]
    assert row["company_name"] is None
    assert row["salary_min"] is None


def test_filters_and_sorting(conn):
    db.upsert_results(conn, [
        {"job_url": "http://x/a", "is_match": True, "company_name": "Acme",
         "job_title": "Data Scientist", "job_type": "Full-time", "salary_min": 120000},
        {"job_url": "http://x/b", "is_match": False, "company_name": "Globex",
         "job_title": "Chef", "job_type": "Part-time", "salary_min": 40000},
        {"job_url": "http://x/c", "is_match": True, "company_name": "Acme",
         "job_title": "AI Lead", "job_type": "Contract", "salary_min": 200000},
    ])
    assert {r["job_url"] for r in db.list_jobs(conn, status="match")} == {"http://x/a", "http://x/c"}
    assert [r["job_url"] for r in db.list_jobs(conn, company="Acme")] == ["http://x/a", "http://x/c"]
    assert [r["job_url"] for r in db.list_jobs(conn, search="chef")] == ["http://x/b"]
    assert [r["job_url"] for r in db.list_jobs(conn, min_salary=150000)] == ["http://x/c"]
    top = db.list_jobs(conn, sort="salary")[0]
    assert top["job_url"] == "http://x/c"
    facets = db.facets(conn)
    assert facets["companies"] == ["Acme", "Globex"]
    assert set(facets["types"]) == {"Full-time", "Part-time", "Contract"}
    counts = db.counts(conn)
    assert counts["match"] == 2 and counts["rejected"] == 1 and counts["total"] == 3


def test_preview_roundtrip(conn):
    db.upsert_seen(conn, ["http://x/p"])
    db.set_preview(conn, "http://x/p", "full page text", "one line summary")
    row = db.list_jobs(conn)[0]
    assert row["preview"] == "full page text"
    assert row["summary"] == "one line summary"


def test_merge_legacy_csvs(tmp_path, monkeypatch):
    import pandas as pd

    cache = tmp_path / "cache"
    cache.mkdir()
    prior = cache / "target_jobs_old.csv"
    pd.DataFrame({"job_url": ["http://x/1", "http://x/2"]}).to_csv(prior, index=False)
    results = tmp_path / "results_output.csv"
    pd.DataFrame([
        {"job_url": "http://x/1", "is_match": True, "company_name": "Acme",
         "job_title": "Eng", "job_type": "Full-time", "salary_min": 100, "salary_max": 200},
    ]).to_csv(results, index=False)

    monkeypatch.setattr(db, "PRIOR_JOBS_CSV", str(prior))
    monkeypatch.setattr(db, "RESULTS_CSV", str(results))
    conn = db.get_conn(str(tmp_path / "jobs.db"))
    assert db.merge_legacy_csvs(conn) is True
    rows = {r["job_url"]: r for r in db.list_jobs(conn)}
    assert rows["http://x/1"]["status"] == "match"
    assert rows["http://x/2"]["status"] == "seen"
    # Second call no-ops (DB already populated).
    assert db.merge_legacy_csvs(conn) is False
    conn.close()
