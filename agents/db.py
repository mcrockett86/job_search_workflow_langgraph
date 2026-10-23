"""SQLite job database — the single source of truth for the web viewer.

The pipeline used to write only ``results_output.csv`` (matches) and
``cache/target_jobs_old.csv`` (seen URLs).  This module keeps a richer
per-job record so the interactive viewer can browse, filter, preview and
export without re-running the scraper.

The DB is created lazily and merged once from the legacy CSVs, so existing
runs show up in the viewer without any manual migration.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, "cache")
DB_PATH = os.path.join(CACHE_DIR, "jobs.db")
PRIOR_JOBS_CSV = os.path.join(CACHE_DIR, "target_jobs_old.csv")
RESULTS_CSV = os.path.join(REPO_ROOT, "results_output.csv")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_url      TEXT PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'seen',  -- seen | match | rejected | error
    is_match     INTEGER,
    company_name TEXT,
    job_title    TEXT,
    job_type     TEXT,
    salary_min   REAL,
    salary_max   REAL,
    summary      TEXT,
    preview      TEXT,
    preview_at   TEXT,
    work_mode    TEXT,  -- remote | hybrid | in_person (NULL = unknown)
    run_count    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_seen   ON jobs(last_seen);
-- idx_jobs_mode is created in _migrate() so it works on legacy DBs too.
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Open (initialize if needed) the jobs database for a profile.

    With no explicit path, uses the *current profile's* DB (see
    agents/profiles.py) so each profile's discovered-job ledger stays
    separate from every other profile's.
    """
    if db_path is None:
        from . import profiles

        profiles.ensure_init()
        db_path = profiles.current().db_path
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created (idempotent)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "work_mode" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN work_mode TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_mode ON jobs(work_mode)")
    conn.commit()
    backfill_work_modes(conn)


def backfill_work_modes(conn: sqlite3.Connection) -> int:
    """Classify rows that have text but no work_mode yet (idempotent)."""
    from .work_mode import classify_work_mode

    rows = conn.execute(
        """SELECT rowid, job_title, summary, preview FROM jobs
           WHERE work_mode IS NULL
             AND (COALESCE(summary,'') <> '' OR COALESCE(preview,'') <> '')"""
    ).fetchall()
    n = 0
    for r in rows:
        mode = classify_work_mode(r["job_title"], r["preview"], r["summary"])
        if mode:
            conn.execute("UPDATE jobs SET work_mode = ? WHERE rowid = ?",
                         (mode, r["rowid"]))
            n += 1
    if n:
        conn.commit()
    return n


def _clean(value: Any) -> Any:
    """Normalize pandas/NaN-ish values to plain SQLite types."""
    if value is None:
        return None
    if isinstance(value, float):
        # NaN check without importing math for the common path
        if value != value:  # noqa: PLR0124
            return None
        return value
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:  # pragma: no cover - pandas always present here
        pass
    if isinstance(value, (int, str)):
        return value
    return str(value)


def upsert_seen(conn: sqlite3.Connection, urls: Iterable[str]) -> int:
    """Record job URLs as seen (does not downgrade match/rejected status)."""
    now = _now()
    count = 0
    for url in urls:
        url = _clean(url)
        if not url:
            continue
        cur = conn.execute(
            "UPDATE jobs SET last_seen = ?, run_count = run_count + 1 WHERE job_url = ?",
            (now, url),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO jobs (job_url, first_seen, last_seen, status) VALUES (?, ?, ?, 'seen')",
                (url, now, now),
            )
        count += 1
    conn.commit()
    return count


def upsert_results(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Upsert screened job rows (match or rejected) with their metadata."""
    from .work_mode import classify_work_mode

    now = _now()
    count = 0
    for row in rows:
        url = _clean(row.get("job_url"))
        if not url:
            continue
        is_match = row.get("is_match")
        if isinstance(is_match, str):
            is_match = is_match.strip().lower() in ("true", "1", "yes")
        status = "match" if is_match else "rejected"
        # Work mode is derived from whatever posting text we have; the
        # existing preview column is backfilled later, so re-derive then too.
        work_mode = row.get("work_mode") or classify_work_mode(
            row.get("job_title"), row.get("job_description"),
            row.get("description"), row.get("summary"), row.get("preview"))
        conn.execute(
            """
            INSERT INTO jobs (job_url, first_seen, last_seen, status, is_match,
                              company_name, job_title, job_type, salary_min, salary_max,
                              summary, work_mode, run_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(job_url) DO UPDATE SET
                last_seen    = excluded.last_seen,
                status       = excluded.status,
                is_match     = excluded.is_match,
                company_name = COALESCE(excluded.company_name, jobs.company_name),
                job_title    = COALESCE(excluded.job_title, jobs.job_title),
                job_type     = COALESCE(excluded.job_type, jobs.job_type),
                salary_min   = COALESCE(excluded.salary_min, jobs.salary_min),
                salary_max   = COALESCE(excluded.salary_max, jobs.salary_max),
                summary      = COALESCE(excluded.summary, jobs.summary),
                work_mode    = COALESCE(excluded.work_mode, jobs.work_mode),
                run_count    = jobs.run_count + 1
            """,
            (
                url, now, now, status, 1 if is_match else 0,
                _clean(row.get("company_name")),
                _clean(row.get("job_title")),
                _clean(row.get("job_type")),
                _clean(row.get("salary_min")),
                _clean(row.get("salary_max")),
                _clean(row.get("summary")),
                _clean(work_mode),
            ),
        )
        count += 1
    conn.commit()
    return count


def set_preview(conn: sqlite3.Connection, url: str, preview: str, summary: str | None) -> None:
    """Cache fetched page text + summary for hover previews."""
    from .work_mode import classify_work_mode

    row = conn.execute(
        "SELECT job_title, work_mode FROM jobs WHERE job_url = ?", (url,)
    ).fetchone()
    # The full page text is the best signal we get for most jobs — fill in
    # work_mode here when screening didn't already determine it.
    mode = classify_work_mode(
        row["job_title"] if row else None, preview, summary) if preview else None
    conn.execute(
        """UPDATE jobs SET preview = ?, summary = COALESCE(?, summary),
           preview_at = ?, work_mode = COALESCE(work_mode, ?) WHERE job_url = ?""",
        (preview, summary, _now(), mode, url),
    )
    conn.commit()


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    company: str | None = None,
    job_type: str | None = None,
    work_mode: str | None = None,
    search: str | None = None,
    min_salary: float | None = None,
    sort: str = "seen",
) -> list[dict[str, Any]]:
    """Query jobs with viewer filters. Returns plain dicts (JSON-safe)."""
    sql = "SELECT * FROM jobs"
    where: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if company:
        where.append("LOWER(company_name) = LOWER(?)")
        params.append(company)
    if job_type:
        where.append("LOWER(job_type) = LOWER(?)")
        params.append(job_type)
    if work_mode and work_mode != "all":
        if work_mode == "unknown":
            where.append("work_mode IS NULL")
        else:
            where.append("work_mode = ?")
            params.append(work_mode)
    if search:
        like = f"%{search}%"
        where.append("(job_title LIKE ? OR company_name LIKE ? OR summary LIKE ? OR preview LIKE ? OR job_url LIKE ?)")
        params += [like] * 5
    if min_salary is not None:
        where.append("COALESCE(salary_max, salary_min, 0) >= ?")
        params.append(min_salary)
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = {
        "seen": "last_seen DESC",
        "title": "job_title COLLATE NOCASE ASC",
        "company": "company_name COLLATE NOCASE ASC",
        "salary": "COALESCE(salary_max, salary_min, 0) DESC",
    }.get(sort, "last_seen DESC")
    # job_url tiebreaker keeps ordering deterministic when timestamps collide
    # (same-second inserts) instead of falling back to rowid luck.
    sql += f" ORDER BY {order}, job_url ASC"
    return [dict(r) for r in conn.execute(sql, params)]


def facets(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Distinct company/type values for the filter dropdowns."""
    companies = [
        r["company_name"]
        for r in conn.execute(
            "SELECT DISTINCT company_name FROM jobs WHERE company_name IS NOT NULL ORDER BY company_name COLLATE NOCASE"
        )
    ]
    types = [
        r["job_type"]
        for r in conn.execute(
            "SELECT DISTINCT job_type FROM jobs WHERE job_type IS NOT NULL ORDER BY job_type COLLATE NOCASE"
        )
    ]
    return {"companies": companies, "types": types}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Status counts for the header badges."""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    out = {r["status"]: r["n"] for r in rows}
    out["total"] = sum(out.values())
    for r in conn.execute(
            "SELECT work_mode, COUNT(*) AS n FROM jobs "
            "WHERE work_mode IS NOT NULL GROUP BY work_mode"):
        out[f"mode_{r['work_mode']}"] = r["n"]
    return out


def merge_legacy_csvs(conn: sqlite3.Connection,
                      prior_csv: str | None = None,
                      results_csv: str | None = None) -> bool:
    """One-time merge of the legacy CSVs into an empty DB.

    Returns True if anything was imported. Safe to call on every startup:
    it no-ops once the DB has rows. Paths default to the module-level
    (pre-profiles) locations; callers pass the profile's own CSVs instead.
    """
    prior_csv = prior_csv or PRIOR_JOBS_CSV
    results_csv = results_csv or RESULTS_CSV
    if conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] > 0:
        return False
    imported = False
    if os.path.isfile(prior_csv):
        try:
            import pandas as pd

            df = pd.read_csv(prior_csv)
            urls = [u for u in df["job_url"].dropna().astype(str) if u.strip()]
            if urls:
                upsert_seen(conn, urls)
                imported = True
                logger.info("Merged %d seen URLs from %s", len(urls), PRIOR_JOBS_CSV)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not merge %s: %s", PRIOR_JOBS_CSV, exc)
    if os.path.isfile(results_csv):
        try:
            import pandas as pd

            df = pd.read_csv(results_csv)
            rows = df.to_dict("records")
            if rows:
                upsert_results(conn, rows)
                imported = True
                logger.info("Merged %d result rows from %s", len(rows), RESULTS_CSV)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not merge %s: %s", RESULTS_CSV, exc)
    return imported
