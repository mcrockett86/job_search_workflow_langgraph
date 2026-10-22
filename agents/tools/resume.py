"""Resume text extraction utilities."""

from __future__ import annotations

import os
from pypdf import PdfReader


def _read_pdf(path: str) -> str:
    """Extract all text from a PDF file."""
    reader = PdfReader(path)
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def get_resume(kind: str = "software") -> str:
    """Return resume text.

    Parameters
    ----------
    kind : {"software", "full"}
        - ``"software"`` — software experience only (default).  When the user
          has uploaded their own resume through the web UI (user_config/
          resume.pdf) that file wins; otherwise the bundled one is used.
        - ``"full"`` — full resume including mechanical engineering.
    """
    if kind != "full":
        from .. import user_config
        custom = user_config.get_resume_pdf_path()
        if custom:
            return _read_pdf(custom)

    module_dir = os.path.dirname(__file__)
    resources = os.path.join(module_dir, "resources")

    if kind == "full":
        pdf_path = os.path.join(resources, "resume_full.pdf")
    else:
        pdf_path = os.path.join(resources, "resume_software.pdf")

    return _read_pdf(pdf_path)


# Backwards-compatible aliases.  Job-finder graph still imports these names.
async def get_resume_software() -> str:
    """Get resume including only software experience."""
    return get_resume("software")


async def get_resume_full() -> str:
    """Get resume including all experience, both software and mechanical engineering."""
    return get_resume("full")