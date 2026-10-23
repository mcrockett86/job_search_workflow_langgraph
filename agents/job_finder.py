"""Entry point for running the job search pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Any

import pandas as pd

from .config import EXCLUDED_COMPANIES


logger = logging.getLogger(__name__)


def _db_upsert_seen(urls: list[str]) -> None:
    """Mirror scraped URLs into the SQLite viewer DB (best-effort)."""
    try:
        from . import profiles
        from .db import get_conn, merge_legacy_csvs, upsert_seen

        profiles.ensure_init()
        p = profiles.current()
        conn = get_conn()
        merge_legacy_csvs(conn, prior_csv=p.prior_csv, results_csv=p.results_csv)
        upsert_seen(conn, urls)
        conn.close()
    except Exception as exc:  # noqa: BLE001 — DB is an additive view, never fatal
        logger.warning("DB seen-upsert failed: %s", exc)


def _db_upsert_results(rows: list[dict[str, Any]]) -> None:
    """Mirror screened rows (with summaries) into the SQLite viewer DB."""
    try:
        from .db import get_conn, upsert_results

        conn = get_conn()
        upsert_results(conn, rows)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB results-upsert failed: %s", exc)


# ---------------------------------------------------------------------------
# Graph creation
# ---------------------------------------------------------------------------
def create_job_graph():
    """Create the job-processing LangGraph (convenience wrapper)."""
    from .graph import create_job_graph as _build

    return _build()


async def run_graph(df: pd.DataFrame, stage=None) -> pd.DataFrame:
    """Process each row in *df* through the job graph.

    Returns the same DataFrame with additional columns populated
    (match metadata plus a heuristic ``summary``/``preview`` for the viewer).

    ``stage(done, total, label)`` is called after each job so the web viewer
    can render a progress bar for the screening phase.
    """
    from .graph import create_job_graph  # re-import to avoid circular top-level import
    from .summary import preview_text, summarize

    graph = create_job_graph()
    total = len(df)

    def _stage(done: int, label: str) -> None:
        if stage:
            try:
                stage(done, total, label)
            except Exception:  # noqa: BLE001 — progress must never break the run
                pass

    for done, (i, row) in enumerate(df.iterrows(), start=1):
        try:
            run_state = {
                "id_run": str(uuid.uuid4()),
                "job_url": row["job_url"],
                "messages": [],
            }

            config = {"configurable": {"thread_id": i}}
            response = await graph.ainvoke(run_state, config, stream_mode="values")

            df.loc[i, "is_match"] = response.get("is_match")
            df.loc[i, "company_name"] = response.get("company_name")
            df.loc[i, "job_title"] = response.get("job_title")
            df.loc[i, "job_type"] = response.get("job_type")
            df.loc[i, "salary_min"] = response.get("salary_min")
            df.loc[i, "salary_max"] = response.get("salary_max")

            # Heuristic summary so the viewer can show content without a
            # second fetch on hover (free, instant, offline).
            desc = response.get("job_description") or ""
            if desc:
                df.loc[i, "summary"] = summarize(desc)
                df.loc[i, "preview"] = preview_text(desc)

        except Exception as exc:  # noqa: BLE001 — log and continue
            logger.error("Error processing row %d (%s): %s", i, row.get("job_url"), exc)
        _stage(done, "screening jobs")

    return df


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def _ensure_cache_dir() -> None:
    """Create the active profile's cache directory if it doesn't exist."""
    from . import profiles

    profiles.ensure_init()
    profiles.current().ensure_dirs()


def _prior_csv_path() -> str:
    from . import profiles

    profiles.ensure_init()
    return profiles.current().prior_csv


def _load_prior_jobs() -> pd.DataFrame | None:
    """Load the profile's job cache, returning ``None`` if missing or empty."""
    path = _prior_csv_path()
    if not os.path.isfile(path):
        return None
    try:
        df = pd.read_csv(path)
        return df if not df.empty else None
    except Exception as exc:  # noqa: BLE001 — corrupt file → treat as empty
        logger.warning("Could not read prior jobs CSV (%s): %s", path, exc)
        return None


def _save_prior_jobs(df: pd.DataFrame) -> None:
    """Write *df* rows to the profile's prior-jobs cache (overwrite)."""
    path = _prior_csv_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
async def main_pipeline(
    urls: list[str] | None = None,
    max_pages: int = 10,
    skip_prompt: bool = False,
    progress=None,
) -> str:
    """Run the full job-search pipeline and return path to output CSV.

    ``progress`` is an optional ``callable(str)`` used by the web UI to show
    live status; it is ignored on the CLI path.
    """

    def say(msg: str) -> None:
        print(msg)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001 — progress must never break the run
                pass

    def stage(info: dict) -> None:
        """Structured progress: {'stage','done','total','label','msg'}.

        Back-compat: a plain-string ``progress`` callback (CLI/tests) gets
        the human line instead of the dict.
        """
        say(info.get("msg") or info.get("label") or "")
        if progress:
            try:
                progress(info)
            except Exception:  # noqa: BLE001
                pass

    _ensure_cache_dir()

    # 1. Scrape LinkedIn for new URLs.
    from .linkedin_scraper import scrape_linkedin_job_urls
    from . import profiles, user_config

    # precedence: explicit arg > user-saved URLs > bundled defaults
    if urls is None:
        url_list = user_config.get_search_urls()
    else:
        url_list = urls
    if user_config.urls_are_custom():
        say(f"Using {len(url_list)} custom search URLs (profile '{profiles.current_slug()}')")
    say("Scraping LinkedIn job URLs …")

    def _scrape_progress(done: int, total: int, label: str) -> None:
        stage({"stage": "scrape", "done": done, "total": total, "label": label,
               "msg": f"Scraping LinkedIn — {label} ({done}/{total} pages)"})

    df_new_jobs = scrape_linkedin_job_urls(url_list, max_pages=max_pages,
                                           progress=_scrape_progress if progress else None)
    df_new_jobs = df_new_jobs.drop_duplicates()

    # Mirror everything we saw into the viewer DB (even if screening fails).
    _db_upsert_seen(df_new_jobs["job_url"].astype(str).tolist())

    # 2. Filter out already-processed jobs.
    df_prior = _load_prior_jobs()
    if df_prior is not None and not df_prior.empty:
        prior_set = set(df_prior["job_url"])
        df_new_filtered = df_new_jobs[~df_new_jobs["job_url"].isin(prior_set)].reset_index(drop=True)
        merged = pd.concat([df_new_filtered, df_prior], ignore_index=True)
    else:
        df_new_filtered = df_new_jobs.reset_index(drop=True)
        merged = df_new_jobs.copy()

    say(f"New jobs detected: {len(df_new_filtered)}")

    # 3. Ask user to confirm (unless --skip-prompt).
    if not skip_prompt and len(df_new_filtered) > 0:
        proceed = input("Continue job search pipeline? Type 'yes' to continue: ").strip().lower()
        if proceed != "yes":
            print("Aborted by user.")
            sys.exit(0)

    # 4. Run the graph on new jobs.
    if len(df_new_filtered) == 0:
        say("No new jobs to process.")
        return ""

    # Add empty columns so downstream code works regardless of graph output shape.
    for col in ("thread_id", "id_run", "is_match", "company_name", "job_title", "job_type", "salary_min", "salary_max", "summary", "preview"):
        if col not in df_new_filtered.columns:
            df_new_filtered[col] = pd.NA

    say("Running job graph …")

    def _graph_stage(done: int, total: int, label: str) -> None:
        stage({"stage": "screen", "done": done, "total": total, "label": label,
               "msg": f"Screening jobs — {done}/{total}"})

    df_results = await run_graph(df_new_filtered,
                                 stage=_graph_stage if progress else None)

    # 5. Filter matches and exclude unwanted companies.
    percent_matches = round(
        (df_results["is_match"].sum() / len(df_new_filtered)) * 100, 1
    )
    say(f"\nPercent matches: {percent_matches}%")

    df_matches = df_results[df_results["is_match"] == True]  # noqa: E712 — bool column comparison
    if EXCLUDED_COMPANIES:
        exclude_lower = [c.lower() for c in EXCLUDED_COMPANIES]
        mask = ~df_matches["company_name"].str.lower().isin(exclude_lower)
        df_matches = df_matches[mask].reset_index(drop=True)

    _save_prior_jobs(merged)
    # Screened rows (matches AND non-matches) go to the DB so the viewer can
    # show the full picture, not just the winners.
    _db_upsert_results(df_results.to_dict("records"))
    from . import profiles

    output_path = profiles.current().results_csv
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df_matches.to_csv(output_path, index=False)
    say(f"Results written to {output_path}")

    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")

    # Parse simple CLI flags.
    import argparse
    parser = argparse.ArgumentParser(description="Job finder agent pipeline.")
    parser.add_argument("--skip-prompt", action="store_true", help="Skip interactive confirmation prompt.")
    args, _ = parser.parse_known_args()

    asyncio.run(main_pipeline(skip_prompt=args.skip_prompt))