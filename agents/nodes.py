"""Graph node implementations for the job-processing workflow."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.messages import HumanMessage

from .config import get_llm
from .tools.resume import get_resume_software


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node: read_job_description
# ---------------------------------------------------------------------------
async def read_job_description(state: dict[str, Any]) -> dict[str, Any]:
    """Fetch a job URL and extract its content as plain text."""
    from agents.tools.scraper import scraper_extract_html_from_url

    job_url = state["job_url"]
    logger.info("Reading job description for %s", job_url)

    html_content_text = await scraper_extract_html_from_url(job_url)

    if not html_content_text:
        logger.warning("Failed to extract content from %s", job_url)

    return {"job_description": html_content_text}


# ---------------------------------------------------------------------------
# Node: classify_job_description
# ---------------------------------------------------------------------------
async def classify_job_description(state: dict[str, Any]) -> dict[str, Any]:
    """Determine whether the job is a good fit for the candidate."""
    llm = get_llm()

    job_description = state["job_description"] or ""
    resume_text = await get_resume_software()

    prompt = (
        "You are a job seeker evaluating roles against your skills.\n\n"
        "Compare the following job description to your resume and determine if it is a good fit.\n"
        "Return ONLY the word 'yes' or 'no'.\n\n"
        f"Job Description:\n{job_description}\n\n"
        f"Resume Content:\n{resume_text}"
    )

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    content = (response.content or "").lower()

    is_match = any(word in content for word in ("yes", "good"))

    return {"is_match": is_match}


# ---------------------------------------------------------------------------
# Node: extract_job_details
# ---------------------------------------------------------------------------
async def extract_job_details(state: dict[str, Any]) -> dict[str, Any]:
    """Extract structured metadata from the job description."""
    llm = get_llm()

    job_description = state["job_description"] or ""

    prompt = (
        "You are an information extraction agent. Extract structured job metadata "
        "from the raw text below and return a JSON object with these fields:\n"
        "- company_name: string | null\n"
        "- job_title: string | null\n"
        "- job_type: string | null (e.g. 'Full-time', 'Part-time', 'Contract')\n"
        "- salary_min: number | null (annual USD)\n"
        "- salary_max: number | null (annual USD)\n\n"
        "Rules:\n"
        "- Return ONLY a JSON string — no markdown, no extra text.\n"
        "- Use null for any field you cannot determine.\n"
        "- If salary is a single value, set both min and max to it.\n"
        "- All salaries must be numeric annual USD.\n\n"
        f"{job_description}"
    )

    response = await llm.ainvoke([HumanMessage(content=prompt)])

    try:
        if response.content:
            # Strip possible markdown code fences
            raw = response.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()

            data: dict[str, Any] = json.loads(raw)
        else:
            raise ValueError("Empty LLM response")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to parse job details JSON — %s", exc)
        return {
            "company_name": None,
            "job_title": None,
            "job_type": None,
            "salary_min": None,
            "salary_max": None,
        }

    # Ensure all expected keys exist (default to None).
    defaults = {"company_name", "job_title", "job_type", "salary_min", "salary_max"}
    missing = defaults - set(data.keys())
    for key in missing:
        data[key] = None

    return data


# ---------------------------------------------------------------------------
# Node: handle_bad_fit / handle_good_fit (no-op loggers)
# ---------------------------------------------------------------------------
def handle_bad_fit(state: dict[str, Any]) -> dict[str, Any]:
    """Log that the job was not a match and pass through state unchanged."""
    logger.info("Job marked as bad fit — is_match=%s", state["is_match"])
    return {}


def handle_good_fit(state: dict[str, Any]) -> dict[str, Any]:
    """Log that the job matched and pass through state unchanged."""
    logger.info("Job marked as good fit — is_match=%s", state["is_match"])
    return {}