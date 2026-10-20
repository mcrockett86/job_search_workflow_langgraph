"""Heuristic job-listing summarizer for hover previews.

Kept LLM-free so previews are instant, offline-capable and free. The raw
page text comes from ``agents.tools.scraper`` (html2text output), which is
noisy: nav crumbs, cookie banners, JSON-LD leftovers. This module trims it
to something a human can scan in two seconds.
"""

from __future__ import annotations

import re

# Lines that are boilerplate on LinkedIn/generic job pages.
_BOILERPLATE = re.compile(
    r"^\s*("
    r"skip to main content|sign in|join now|log in|login|"
    r"cookie policy|privacy policy|terms|help center|"
    r"linkedin.*linkedin|©|all rights reserved|"
    r"show more|show less|see more|see less|"
    r"report this job|flag as inappropriate|"
    r"seniority level|employment type|job functions|industries|"
    r"refer to the company's job listing|"
    r"get notified about new"
    r")",
    re.IGNORECASE,
)

# A sentence-ish chunk: at least a few words of prose.
_MIN_SENTENCE_CHARS = 40
_MAX_SUMMARY_SENTENCES = 3
_MAX_SUMMARY_CHARS = 420
_PREVIEW_CHARS = 2200


def _clean_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip("#").strip()
        # html2text escapes some chars; unescape the common ones.
        line = line.replace("\\b", "").replace("\\*", "").replace("\\_", "_")
        if len(line) < 3:
            continue
        if _BOILERPLATE.match(line):
            continue
        # Drop markdown link syntax: [text](url) -> text
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        # Drop bare URLs
        line = re.sub(r"https?://\S+", "", line).strip()
        if len(line) < 3:
            continue
        out.append(line)
    return out


def summarize(text: str | None) -> str:
    """Return a 1-3 sentence summary of the listing's opening content."""
    if not text:
        return ""
    lines = _clean_lines(text)
    # Prefer lines that look like prose (contain a space and some length).
    prose = [ln for ln in lines if " " in ln and len(ln) >= _MIN_SENTENCE_CHARS]
    if not prose:
        prose = lines[:_MAX_SUMMARY_SENTENCES]
    summary = " ".join(prose[:_MAX_SUMMARY_SENTENCES]).strip()
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    return summary


def preview_text(text: str | None) -> str:
    """Return cleaned page text trimmed for a hover-preview card."""
    if not text:
        return ""
    lines = _clean_lines(text)
    joined = "\n".join(lines)
    if len(joined) > _PREVIEW_CHARS:
        joined = joined[:_PREVIEW_CHARS].rsplit("\n", 1)[0] + "\n…"
    return joined


def guess_title_company(text: str | None) -> tuple[str | None, str | None]:
    """Best-effort (title, company) from raw page text / og tags.

    Used only as a fallback when the LLM extraction didn't run.
    """
    if not text:
        return None, None
    # html2text keeps <title> as the first non-empty line usually.
    for line in _clean_lines(text)[:6]:
        # "Job Title at Company" or "Company: Job Title"
        m = re.match(r"^(.{4,90}?)\s+at\s+([A-Z][\w&.'\- ]{2,60})$", line)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        m = re.match(r"^([A-Z][\w&.'\- ]{2,60}):\s+(.{4,90})$", line)
        if m:
            return m.group(2).strip(), m.group(1).strip()
    return None, None
