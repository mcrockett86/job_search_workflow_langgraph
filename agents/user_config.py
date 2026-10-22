"""Per-user search configuration: custom resume PDF + custom search URLs.

Everything a user configures through the web UI is cached in local files under
``user_config/`` (git-ignored). When a file is absent the bundled defaults from
``agents.config`` apply, so the app keeps working out of the box with the
owner's software-engineer resume and LinkedIn queries.

Resolution order
----------------
resume PDF : user_config/resume.pdf        -> agents/tools/resources/resume_software.pdf
search URLs: user_config/search_urls.txt   -> config.LINKEDIN_URLS
"""

from __future__ import annotations

import os
import time

from . import config

# Repo root = parent of the agents/ package directory.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_DIR = os.path.join(ROOT, "user_config")
USER_RESUME_PDF = os.path.join(USER_DIR, "resume.pdf")
USER_URLS_FILE = os.path.join(USER_DIR, "search_urls.txt")

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB is generous for a resume
PDF_MAGIC = b"%PDF"


# ---------------------------------------------------------------------------
# Search URLs
# ---------------------------------------------------------------------------
def parse_urls(raw: str) -> list[str]:
    """One URL per line (blank lines and # comments ignored)."""
    out: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def validate_urls(urls: list[str]) -> list[str]:
    """Return the list if every entry is http(s); raise ValueError otherwise."""
    if not urls:
        raise ValueError("No URLs provided — add at least one search URL.")
    bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
    if bad:
        shown = ", ".join(bad[:3]) + (" …" if len(bad) > 3 else "")
        raise ValueError(f"URLs must start with http:// or https:// — problem lines: {shown}")
    return urls


def save_urls(urls: list[str]) -> int:
    """Overwrite the cached URL list. Returns the number saved."""
    urls = validate_urls(list(urls))
    os.makedirs(USER_DIR, exist_ok=True)
    header = (
        "# Job-search URLs, one per line (lines starting with # are ignored).\n"
        f"# Saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    with open(USER_URLS_FILE, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(urls) + "\n")
    return len(urls)


def get_search_urls() -> list[str]:
    """Custom URLs when the user has saved some, else the bundled defaults."""
    if os.path.isfile(USER_URLS_FILE):
        try:
            with open(USER_URLS_FILE, encoding="utf-8") as fh:
                urls = parse_urls(fh.read())
            if urls:
                return urls
        except OSError:
            pass
    return list(config.LINKEDIN_URLS)


def urls_are_custom() -> bool:
    return os.path.isfile(USER_URLS_FILE)


# ---------------------------------------------------------------------------
# Resume PDF
# ---------------------------------------------------------------------------
def save_resume_pdf(data: bytes, original_name: str = "") -> dict:
    """Validate and cache an uploaded resume PDF (overwrites the previous one)."""
    if not data or not data.lstrip()[:4] == PDF_MAGIC:
        raise ValueError("File does not look like a PDF — please upload a real .pdf export.")
    if len(data) > MAX_RESUME_BYTES:
        raise ValueError(f"PDF too large ({len(data) // 1024 // 1024} MB) — limit is "
                         f"{MAX_RESUME_BYTES // 1024 // 1024} MB.")
    os.makedirs(USER_DIR, exist_ok=True)
    tmp = USER_RESUME_PDF + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    # sanity check: pypdf must be able to open it before we replace the old one
    from pypdf import PdfReader
    try:
        reader = PdfReader(tmp)
        pages = len(reader.pages)
        text = "".join((p.extract_text() or "") for p in reader.pages[:2])
    except Exception as exc:  # noqa: BLE001
        os.remove(tmp)
        raise ValueError(f"Could not parse the PDF ({exc}). Export it again from your editor.") from exc
    if not text.strip():
        os.remove(tmp)
        raise ValueError("No selectable text found in the PDF — matching needs a "
                         "text resume, not a scanned image.")
    os.replace(tmp, USER_RESUME_PDF)
    meta = {
        "filename": os.path.basename(original_name or "resume.pdf"),
        "pages": pages,
        "bytes": len(data),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(USER_DIR, "resume_meta.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(f"{k}={v}" for k, v in meta.items()))
    return meta


def get_resume_pdf_path() -> str | None:
    """Path to the user's uploaded resume, or None when using the bundled one."""
    return USER_RESUME_PDF if os.path.isfile(USER_RESUME_PDF) else None


def resume_meta() -> dict | None:
    path = os.path.join(USER_DIR, "resume_meta.txt")
    if not os.path.isfile(USER_RESUME_PDF):
        return None
    meta: dict[str, str] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        meta[k] = v
        except OSError:
            pass
    meta.setdefault("filename", "resume.pdf")
    meta["path"] = USER_RESUME_PDF
    return meta


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
def reset(which: str = "all") -> list[str]:
    """Delete cached user files so bundled defaults apply again.

    Returns the list of things reset.
    """
    reset_items: list[str] = []
    targets = {
        "resume": [USER_RESUME_PDF, os.path.join(USER_DIR, "resume_meta.txt")],
        "urls": [USER_URLS_FILE],
    }
    files: list[str] = []
    if which == "all":
        files = targets["resume"] + targets["urls"]
    elif which in targets:
        files = targets[which]
    else:
        raise ValueError(f"Unknown reset target: {which!r} (use resume|urls|all)")
    for f in files:
        if os.path.isfile(f):
            os.remove(f)
    if which in ("resume", "all"):
        reset_items.append("resume")
    if which in ("urls", "all"):
        reset_items.append("urls")
    return reset_items


# ---------------------------------------------------------------------------
# Status for the UI
# ---------------------------------------------------------------------------
def status() -> dict:
    custom_urls = os.path.isfile(USER_URLS_FILE)
    urls = get_search_urls()
    rmeta = resume_meta()
    return {
        "resume": {
            "custom": rmeta is not None,
            "filename": (rmeta or {}).get("filename"),
            "pages": (rmeta or {}).get("pages"),
            "saved_at": (rmeta or {}).get("saved_at"),
            "default_path": config.RESUME_SOFTWARE_PDF,
        },
        "urls": {
            "custom": custom_urls,
            "count": len(urls),
            "urls": urls,
        },
    }
