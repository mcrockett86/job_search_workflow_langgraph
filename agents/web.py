"""Interactive job viewer + pipeline runner (local web app).

Run with:  uv run python -m agents.web   (then open http://127.0.0.1:8734)

Endpoints
---------
GET  /                  viewer page
GET  /api/jobs          filtered job list (JSON)
GET  /api/facets        filter options + status counts
POST /api/run           start the scrape→screen pipeline (background)
GET  /api/run/status    live run log / progress
GET  /api/preview       fetch + summarize one listing on demand (cached)
GET  /api/export        CSV or Markdown export of the filtered set

The server binds to 127.0.0.1 by default: it can trigger a run that uses
your LinkedIn credentials, so it must never be exposed to the network.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import db, profiles, user_config
from .summary import guess_title_company, preview_text, summarize

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_HTML = os.path.join(HERE, "viewer.html")

app = FastAPI(title="Job Search Viewer", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Profile scoping: ?profile=<slug> (or X-Profile header) selects the active
# profile for the request; everything downstream (config, DB, caches) reads
# it from the contextvar. Unknown slugs are rejected up front.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def profile_middleware(request: Request, call_next):
    slug = request.query_params.get("profile") or request.headers.get("x-profile")
    token = None
    if slug:
        try:
            slug = profiles.slugify(slug)
            profiles.ensure_init()
            profiles.get(slug)
        except (KeyError, ValueError):
            return JSONResponse({"detail": f"Unknown profile: {slug}"},
                                status_code=404)
        token = profiles.set_current(slug)
    try:
        return await call_next(request)
    finally:
        if token is not None:
            profiles.reset_current(token)


def _open_db() -> "db.sqlite3.Connection":
    """Current profile's DB, with its legacy CSVs merged in if needed."""
    conn = db.get_conn()
    p = profiles.current()
    db.merge_legacy_csvs(conn, prior_csv=p.prior_csv, results_csv=p.results_csv)
    return conn


# ---------------------------------------------------------------------------
# Run state (single pipeline at a time — LinkedIn scraping is not concurrent-safe)
# ---------------------------------------------------------------------------
class RunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.last_result: str | None = None
        self.profile: str | None = None

    def append(self, msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        for line in str(msg).splitlines() or [""]:
            self.log.append(f"[{stamp}] {line}")
        # keep the log bounded
        if len(self.log) > 500:
            del self.log[: len(self.log) - 500]

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "last_result": self.last_result,
            "profile": self.profile,
            "log": self.log[-120:],
        }


run_state = RunState()


def _run_pipeline_in_thread(profile_slug: str | None = None) -> None:
    """Background worker: runs main_pipeline in a private event loop.

    Threads start with a fresh contextvar context, so the profile that was
    active when /api/run was called is re-set here explicitly — the whole
    run (URLs, resume, seen-job ledger, DB) then resolves to that profile.
    """

    def progress(msg: str) -> None:
        run_state.append(msg)

    async def _go() -> None:
        from .job_finder import main_pipeline

        await main_pipeline(skip_prompt=True, progress=progress)

    if profile_slug:
        profiles.set_current(profile_slug)
    try:
        asyncio.run(_go())
        run_state.last_result = "done"
    except SystemExit:  # pipeline aborts via sys.exit on user decline (not here)
        run_state.last_result = "aborted"
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        run_state.error = f"{type(exc).__name__}: {exc}"
        run_state.append(f"ERROR: {run_state.error}")
        logger.exception("pipeline run failed")
    finally:
        run_state.running = False
        run_state.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
def api_jobs(
    status: str = Query("all"),
    company: str | None = None,
    job_type: str | None = Query(None),
    search: str | None = None,
    min_salary: float | None = Query(None, ge=0),
    sort: str = Query("seen"),
) -> JSONResponse:
    conn = _open_db()
    rows = db.list_jobs(
        conn, status=status, company=company, job_type=job_type,
        search=search, min_salary=min_salary, sort=sort,
    )
    # previews are large; the list only needs the short summary.
    for r in rows:
        r.pop("preview", None)
    conn.close()
    return JSONResponse({"jobs": rows})


@app.get("/api/facets")
def api_facets() -> JSONResponse:
    conn = _open_db()
    out = {"facets": db.facets(conn), "counts": db.counts(conn)}
    conn.close()
    return JSONResponse(out)


@app.post("/api/run")
def api_run() -> JSONResponse:
    if run_state.running:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    # Auth resolution mirrors linkedin_scraper: a persistent profile (no
    # password anywhere) satisfies the gate just as well as env credentials.
    from . import linkedin_scraper  # lazy: keeps selenium off the import path
    if not linkedin_scraper.profile_dir_from_env():
        missing = [v for v in ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD") if not os.environ.get(v)]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Missing LinkedIn auth: {', '.join(missing)} unset and no "
                    "LINKEDIN_PROFILE_DIR set. Recommended: sign in once with "
                    "`python -m agents.linkedin_login` (passwordless profile) "
                    "and set LINKEDIN_PROFILE_DIR."
                ),
            )
    run_profile = profiles.current_slug()
    with run_state.lock:
        if run_state.running:
            raise HTTPException(status_code=409, detail="A run is already in progress")
        run_state.running = True
        run_state.log = []
        run_state.error = None
        run_state.profile = run_profile
        run_state.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run_state.finished_at = None
        run_state.append(f"Run started (profile: {run_profile})")
        t = threading.Thread(target=_run_pipeline_in_thread, args=(run_profile,),
                             daemon=True)
        t.start()
    return JSONResponse({"ok": True, "profile": run_profile})


@app.get("/api/run/status")
def api_run_status() -> JSONResponse:
    return JSONResponse(run_state.snapshot())


@app.get("/api/preview")
def api_preview(url: str = Query(..., min_length=8)) -> JSONResponse:
    """Return cached preview text, fetching + summarizing once if needed."""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE job_url = ?", (url,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Unknown job URL")
    if row["preview"]:
        out = {"preview": row["preview"], "summary": row["summary"], "cached": True}
        conn.close()
        return JSONResponse(out)
    conn.close()

    # Lazy fetch (public pages only; LinkedIn-gated pages will fail — the UI
    # then just shows the link).
    from .tools.scraper import scraper_extract_html_from_url

    try:
        text = asyncio.run(scraper_extract_html_from_url(url))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}") from exc
    if not text:
        raise HTTPException(status_code=502, detail="Could not fetch page content")

    prev = preview_text(text)
    summ = summarize(text)
    conn = db.get_conn()
    title, company = guess_title_company(text)
    db.set_preview(conn, url, prev, summ)
    if title or company:
        conn.execute(
            "UPDATE jobs SET job_title = COALESCE(job_title, ?), company_name = COALESCE(company_name, ?) WHERE job_url = ?",
            (title, company, url),
        )
        conn.commit()
    conn.close()
    return JSONResponse({"preview": prev, "summary": summ, "cached": False})


def _filtered_rows(request: Request) -> list[dict[str, Any]]:
    q = request.query_params
    conn = _open_db()
    rows = db.list_jobs(
        conn,
        status=q.get("status", "all"),
        company=q.get("company") or None,
        job_type=q.get("job_type") or None,
        search=q.get("search") or None,
        min_salary=float(q["min_salary"]) if q.get("min_salary") else None,
        sort=q.get("sort", "seen"),
    )
    conn.close()
    return rows


@app.get("/api/export")
def api_export(request: Request, format: str = Query("csv", pattern="^(csv|md)$")) -> StreamingResponse:
    """Export the filtered set with summaries — review before opening links."""
    rows = _filtered_rows(request)
    stamp = time.strftime("%Y-%m-%d")
    if format == "md":
        buf = io.StringIO()
        buf.write(f"# Job matches — {stamp}\n\n")
        for r in rows:
            salary = _salary_str(r)
            buf.write(f"## {r.get('job_title') or 'Untitled'} — {r.get('company_name') or 'Unknown'}\n")
            buf.write(
                f"- status: {r['status']} | type: {r.get('job_type') or '?'} | salary: {salary} | seen: {r['last_seen'][:10]}\n"
            )
            if r.get("summary"):
                buf.write(f"- summary: {r['summary']}\n")
            buf.write(f"- link: {r['job_url']}\n\n")
        body = buf.getvalue().encode()
        media = "text/markdown"
        fname = f"jobs_{stamp}.md"
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["job_title", "company_name", "job_type", "salary_min", "salary_max",
                    "status", "summary", "last_seen", "job_url"])
        for r in rows:
            w.writerow([r.get("job_title"), r.get("company_name"), r.get("job_type"),
                        r.get("salary_min"), r.get("salary_max"), r["status"],
                        r.get("summary"), r["last_seen"], r["job_url"]])
        body = buf.getvalue().encode()
        media = "text/csv"
        fname = f"jobs_{stamp}.csv"
    return StreamingResponse(
        io.BytesIO(body),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _salary_str(r: dict[str, Any]) -> str:
    lo, hi = r.get("salary_min"), r.get("salary_max")
    if lo and hi and lo != hi:
        return f"${lo:,.0f}–${hi:,.0f}"
    if lo or hi:
        return f"${(lo or hi):,.0f}"
    return "—"


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
class ProfileIn(BaseModel):
    name: str


@app.get("/api/profiles")
def api_profiles() -> JSONResponse:
    """All profiles + which one is the server-side default."""
    profiles.ensure_init()
    return JSONResponse({"profiles": profiles.list_profiles(),
                         "default": profiles.default_slug()})


@app.post("/api/profiles")
def api_profiles_create(body: ProfileIn) -> JSONResponse:
    """Create a new profile (fresh resume/URLs/history)."""
    try:
        p = profiles.create(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "slug": p.slug})


# ---------------------------------------------------------------------------
# Search configuration (resume + URLs)
# ---------------------------------------------------------------------------
class UrlsIn(BaseModel):
    urls: list[str]


@app.get("/api/config")
def api_config() -> JSONResponse:
    """Current resume + search-URL configuration (custom vs defaults)."""
    return JSONResponse(user_config.status())


@app.post("/api/config/resume")
async def api_config_resume(file: UploadFile = File(...)) -> JSONResponse:
    """Upload a resume PDF; cached to user_config/resume.pdf (overwrites)."""
    data = await file.read()
    try:
        meta = user_config.save_resume_pdf(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta.pop("path", None)
    return JSONResponse({"ok": True, "resume": meta})


@app.post("/api/config/urls")
def api_config_urls(body: UrlsIn) -> JSONResponse:
    """Save the search-URL list (one per line in the textarea); overwrites."""
    try:
        n = user_config.save_urls(body.urls)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "saved": n})


@app.post("/api/config/reset")
def api_config_reset(which: str = Query("all", pattern="^(resume|urls|all)$")) -> JSONResponse:
    """Delete cached user files and fall back to the bundled defaults."""
    items = user_config.reset(which)
    return JSONResponse({"ok": True, "reset": items})


# ---------------------------------------------------------------------------
# Viewer page
# ---------------------------------------------------------------------------
@app.get("/")
def viewer() -> FileResponse:
    return FileResponse(VIEWER_HTML, media_type="text/html")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
    # Load .env / dotenv.env so the Run button sees LinkedIn credentials
    # (agents.config does this lazily, but the /api/run pre-check runs first).
    from . import config as _config  # noqa: F401 — import for dotenv side effect

    host = os.environ.get("JOB_VIEWER_HOST", "127.0.0.1")
    port = int(os.environ.get("JOB_VIEWER_PORT", "8734"))
    profiles.ensure_init()
    conn = _open_db()
    conn.close()
    print(f"Job viewer → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
