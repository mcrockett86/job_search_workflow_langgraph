"""Classify a job posting as remote / hybrid / in_person from its text.

Pure heuristics (no LLM, no network): cheap, deterministic, testable, and
good enough for a *filter* on jobs the screener already ranked. Unknown text
returns None so the row simply doesn't match any work-mode filter instead of
being guessed into a bucket.

Precedence matters: a posting that says "hybrid — 3 days in office, remote
the rest" mentions both remote and on-site, so hybrid markers are checked
first, then explicit remote, then on-site.
"""

from __future__ import annotations

import re

REMOTE = "remote"
HYBRID = "hybrid"
IN_PERSON = "in_person"

# Word-boundary-aware phrase matching (short tokens like "wfh" must not
# match inside longer words).
def _re(phrase: str) -> re.Pattern:
    p = re.escape(phrase).replace(r"\ ", r"[\s\-]+")
    return re.compile(rf"(?<![a-z0-9]){p}(?![a-z0-9])")


_HYBRID = [
    "hybrid",
    "hybride",  # French-Canadian postings
    "2 days in the office",
    "3 days in the office",
    "2 days in office",
    "3 days in office",
    "2 days on site",
    "3 days on site",
    "two days in the office",
    "three days in the office",
    "days per week in the office",
    "days a week in the office",
    "mix of remote",
    "mix of office",
    "part remote",
    "part-time remote",
    "remote and in office",
    "remote and on site",
    "some remote",
    "partial remote",
    "flexible schedule in office",
]

_REMOTE = [
    "remote",
    "fully remote",
    "100% remote",
    "work from home",
    "work from anywhere",
    "wfh",
    "anywhere in the us",
    "anywhere in the us.",
    "remote first",
    "remote-first",
    "distributed team",
    "distributed company",
    "location independent",
    "location-independent",
    "telecommute",
    "telecommuting",
    "virtual role",
    "work remotely",
    "remote only",
]

_IN_PERSON = [
    "on-site",
    "onsite",
    "on site",
    "in office",
    "in the office",
    "must be in the office",
    "no remote",
    "not remote",
    "no remote work",
    "this is not a remote",
    "relocation assistance",
    "relocation provided",
    "willingness to travel 100%",
    "on premises",
    "on-premises",
    "at our office",
    "at the office",
]

_HYBRID_RES = [_re(p) for p in _HYBRID]
_REMOTE_RES = [_re(p) for p in _REMOTE]
_IN_PERSON_RES = [_re(p) for p in _IN_PERSON]

# Explicit negations flip an otherwise-remote posting to on-site.
_NEGATIVE_REMOTE = [
    "not remote",
    "no remote",
    "not a remote",
    "this role is not remote",
    "must be in the office",
    "no remote work",
    "unfortunately, this is not remote",
]
_NEGATIVE_REMOTE_RES = [_re(p) for p in _NEGATIVE_REMOTE]


def classify_work_mode(*texts: str | None) -> str | None:
    """Return REMOTE / HYBRID / IN_PERSON, or None when undecidable.

    Pass whatever text you have (title, description, summary, preview);
    they are joined and scanned once.
    """
    blob = " \n ".join(t for t in texts if t).lower()
    if not blob.strip():
        return None

    negated = any(r.search(blob) for r in _NEGATIVE_REMOTE_RES)
    hybrid = any(r.search(blob) for r in _HYBRID_RES)
    remote = any(r.search(blob) for r in _REMOTE_RES)
    onsite = any(r.search(blob) for r in _IN_PERSON_RES)

    if hybrid:
        return HYBRID
    if negated:
        return IN_PERSON
    if remote:
        return REMOTE
    if onsite:
        return IN_PERSON
    return None


def work_mode_counts(rows) -> dict[str, int]:
    """Tally a work_mode key across dicts (used by tests/tools)."""
    out: dict[str, int] = {}
    for r in rows:
        m = r.get("work_mode") if hasattr(r, "get") else None
        if m:
            out[m] = out.get(m, 0) + 1
    return out
