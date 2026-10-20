"""Entry point for running the job search pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Any

import pandas as pd

from .config import CACHE_DIR, EXCLUDED_COMPANIES, PRIOR_JOBS_CSV, RESULTS_CSV, LINKEDIN_URLS


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph creation
# ---------------------------------------------------------------------------
def create_job_graph():
    """Create the job-processing LangGraph (convenience wrapper)."""
    from .graph import create_job_graph as _build

    return _build()


async def run_graph(df: pd.DataFrame) -> pd.DataFrame:
    """Process each row in *df* through the job graph.

    Returns the same DataFrame with additional columns populated.
    """
    from .graph import create_job_graph  # re-import to avoid circular top-level import

    graph = create_job_graph()

    for i, row in df.iterrows():
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

        except Exception as exc:  # noqa: BLE001 — log and continue
            logger.error("Error processing row %d (%s): %s", i, row.get("job_url"), exc)

    return df


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def _ensure_cache_dir() -> None:
    """Create the cache directory if it doesn't exist."""
    os.makedirs(CACHE_DIR, exist_ok=True)


def _load_prior_jobs() -> pd.DataFrame | None:
    """Load prior job cache, returning ``None`` if missing or empty."""
    if not os.path.isfile(PRIOR_JOBS_CSV):
        return None
    try:
        df = pd.read_csv(PRIOR_JOBS_CSV)
        return df if not df.empty else None
    except Exception as exc:  # noqa: BLE001 — corrupt file → treat as empty
        logger.warning("Could not read prior jobs CSV (%s): %s", PRIOR_JOBS_CSV, exc)
        return None


def _save_prior_jobs(df: pd.DataFrame) -> None:
    """Append *df* rows to the prior-jobs cache."""
    df.to_csv(PRIOR_JOBS_CSV, index=False)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
async def main_pipeline(
    urls: list[str] | None = None,
    max_pages: int = 10,
    skip_prompt: bool = False,
) -> str:
    """Run the full job-search pipeline and return path to output CSV."""

    _ensure_cache_dir()

    # 1. Scrape LinkedIn for new URLs.
    from .linkedin_scraper import scrape_linkedin_job_urls

    url_list = urls if urls is not None else LINKEDIN_URLS
    print("Scraping LinkedIn job URLs …")
    df_new_jobs = scrape_linkedin_job_urls(url_list, max_pages=max_pages)
    df_new_jobs = df_new_jobs.drop_duplicates()

    # 2. Filter out already-processed jobs.
    df_prior = _load_prior_jobs()
    if df_prior is not None and not df_prior.empty:
        prior_set = set(df_prior["job_url"])
        df_new_filtered = df_new_jobs[~df_new_jobs["job_url"].isin(prior_set)].reset_index(drop=True)
        merged = pd.concat([df_new_filtered, df_prior], ignore_index=True)
    else:
        df_new_filtered = df_new_jobs.reset_index(drop=True)
        merged = df_new_jobs.copy()

    print(f"New jobs detected: {len(df_new_filtered)}")

    # 3. Ask user to confirm (unless --skip-prompt).
    if not skip_prompt and len(df_new_filtered) > 0:
        proceed = input("Continue job search pipeline? Type 'yes' to continue: ").strip().lower()
        if proceed != "yes":
            print("Aborted by user.")
            sys.exit(0)

    # 4. Run the graph on new jobs.
    if len(df_new_filtered) == 0:
        print("No new jobs to process.")
        return ""

    # Add empty columns so downstream code works regardless of graph output shape.
    for col in ("thread_id", "id_run", "is_match", "company_name", "job_title", "job_type", "salary_min", "salary_max"):
        if col not in df_new_filtered.columns:
            df_new_filtered[col] = pd.NA

    print("Running job graph …")
    df_results = await run_graph(df_new_filtered)

    # 5. Filter matches and exclude unwanted companies.
    percent_matches = round(
        (df_results["is_match"].sum() / len(df_new_filtered)) * 100, 1
    )
    print(f"\nPercent matches: {percent_matches}%")

    df_matches = df_results[df_results["is_match"] == True]  # noqa: E712 — bool column comparison
    if EXCLUDED_COMPANIES:
        exclude_lower = [c.lower() for c in EXCLUDED_COMPANIES]
        mask = ~df_matches["company_name"].str.lower().isin(exclude_lower)
        df_matches = df_matches[mask].reset_index(drop=True)

    _save_prior_jobs(merged)
    output_path = RESULTS_CSV
    df_matches.to_csv(output_path, index=False)
    print(f"Results written to {output_path}")

    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")

    # Parse simple CLI flags.
    import argparse
    parser = argparse.ArgumentParser(description="Job finder agent pipeline.")
    parser.add_argument("--skip-prompt", action="store_true", help="Skip interactive confirmation prompt.")
    args, _ = parser.parse_known_args()

    asyncio.run(main_pipeline(skip_prompt=args.skip_prompt))