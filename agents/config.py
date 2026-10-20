"""Central configuration for the job finder agent."""

import os
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
# Try common dotenv locations; fall back to system env.
_load_dotenv_paths = [".env", "dotenv.env"]
for _p in _load_dotenv_paths:
    if os.path.isfile(_p):
        load_dotenv(_p, override=True)  # type: ignore[call-arg]
        break

# ---------------------------------------------------------------------------
# LinkedIn search queries (URL template + params).
# These can be overridden via environment variables.
# ---------------------------------------------------------------------------
LINKEDIN_JOBS_BASE = "https://www.linkedin.com/jobs/search/"


def _build_params(
    *,
    keywords: list[str],
    geo_id: str | int,
    location_name: str,
    remote_only: bool = False,
) -> dict[str, str]:
    """Helper to build the standard LinkedIn search params."""
    return {
        "keywords": "+".join(keywords),
        "geoId": str(geo_id),
        "f_E": "2%2C4",  # Full-time / Part-time
        "f_SB2": "6",  # Mid-senior level
        "f_TPR": "r604800",  # Past week
        "refresh": "true",
        "sortBy": "R",
    }


# Default LinkedIn search queries, one per row.
# Format: (label, url_template_or_none, build_params_kwargs)
# When url is None we use _build_params to construct it dynamically.

DEFAULT_SEARCHES = [
    # Remote — all US locations
    ("Remote - Data Scientist", None, {"keywords": ["data%20scientist"], "geoId": 103644278, "remote_only": True}),
    ("Remote - AI", None, {"keywords": ["artificial%20intelligence"], "geoId": 103644278, "remote_only": True}),
    ("Remote - ML", None, {"keywords": ["machine%20learning"], "geoId": 103644278, "remote_only": True}),
]

# Manually curated full URLs (preserved from original code).
LINKEDIN_URLS: list[str] = [
    # Remote only (past week) — Seattle area codes as proxy for US remote
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_SB2=6&f_TPR=r604800&f_WT=2&geoId=103644278&keywords=data%20scientist&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_SB2=6&f_TPR=r604800&f_WT=2&geoId=103644278&keywords=artificial%20intelligence&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_SB2=6&f_TPR=r604800&f_WT=2&geoId=103644278&keywords=machine%20learning&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    # Seattle, WA (past week) — Bellevue/Kirkland/Seattle
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=104116203%2C106619589%2C104145663&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=103644278&keywords=data%20scientist&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=104116203%2C106619589%2C104145663&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=103644278&keywords=artificial%20intelligence&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=104116203%2C106619589%2C104145663&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=103644278&keywords=machine%20learning&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    # San Diego (past week)
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=103918656&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=90010472&keywords=data%20scientist&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=103918656&f_SB2=6&f_TPR=r604800&f_WT=2%2C1%2C3&geoId=90010472&keywords=artificial%20intelligence&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=103918656&f_SB2=6&f_TPR=r604800&f_WT=2%2C1%2C3&geoId=90010472&keywords=machine%20learning&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    # San Francisco Bay Area (past week)
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=102277331&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=90000084&keywords=data%20scientist&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=102277331&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=90000084&keywords=artificial%20intelligence&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
    f"{LINKEDIN_JOBS_BASE}?f_E=2%2C4&f_PP=102277331&f_SB2=6&f_TPR=r604800&f_WT=1%2C3%2C2&geoId=90000084&keywords=machine%20learning&origin=JOB_SEARCH_PAGE_JOB_FILTER&refresh=true&sortBy=R",
]

# Companies to exclude from results.
EXCLUDED_COMPANIES: list[str] = ["harnham"]


# ---------------------------------------------------------------------------
# LLM singleton — created once and reused across all graph nodes.
# ---------------------------------------------------------------------------
_LLM_CACHE: dict | None = None


def get_llm() -> "ChatOpenAI":
    """Return a cached LangChain ChatOpenAI instance (gpt-4o-mini, temp=0)."""
    global _LLM_CACHE  # noqa: PLW0603
    if _LLM_CACHE is None:
        _LLM_CACHE = init_chat_model(model="openai:gpt-4o-mini", temperature=0.0)
    return _LLM_CACHE


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CACHE_DIR = "cache"
PRIOR_JOBS_CSV = os.path.join(CACHE_DIR, "target_jobs_old.csv")
RESULTS_CSV = "results_output.csv"

# Resume PDF paths (relative to tools/resources/).
RESUME_SOFTWARE_PDF = "agents/tools/resources/resume_software.pdf"
RESUME_FULL_PDF = "agents/tools/resources/resume_full.pdf"