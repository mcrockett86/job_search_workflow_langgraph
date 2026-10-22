"""Per-user search configuration: custom resume PDF + custom search URLs.

Everything a user configures through the web UI is cached in local files under
``user_config/`` (git-ignored). When a file is absent the bundled defaults from
``agents.config`` apply, so the app keeps working out of the box with the
owner's software-engineer resume and LinkedIn queries.

Resolution order
----------------
resume PDF : user_config/profiles/<profile>/resume.pdf -> agents/tools/resources/resume_software.pdf
search URLs: user_config/profiles/<profile>/search_urls.txt -> config.LINKEDIN_URLS

All paths are per-profile (see agents/profiles.py); the "current profile" is
a contextvar set by the web layer per request / per background run.
"""

from __future__ import annotations

import os
import time

from . import config
from . import profiles as _profiles

# Repo root = parent of the agents/ package directory.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_DIR = os.path.join(ROOT, "user_config")
# Legacy flat locations (pre-profiles) — kept for tests/overrides.
USER_RESUME_PDF = os.path.join(USER_DIR, "resume.pdf")
USER_URLS_FILE = os.path.join(USER_DIR, "search_urls.txt")

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB is generous for a resume
PDF_MAGIC = b"%PDF"

# Test hook: when set, all paths resolve here instead of the profile dirs.
_override_dir: str | None = None


def _paths():
    """(dir, resume_pdf, resume_meta, urls_file) for the active target."""
    if _override_dir:
        d = _override_dir
        return d, (os.path.join(d, "resume.pdf"), os.path.join(d, "resume_meta.txt"),
                   os.path.join(d, "search_urls.txt"))
    p = _profiles.current()
    return p.config_dir, (p.resume_pdf, p.resume_meta, p.urls_file)


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
    d, (_, _, urls_file) = _paths()
    os.makedirs(d, exist_ok=True)
    header = (
        "# Job-search URLs, one per line (lines starting with # are ignored).\n"
        f"# Saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    with open(urls_file, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(urls) + "\n")
    return len(urls)


def get_search_urls() -> list[str]:
    """Custom URLs when the profile has saved some, else the bundled defaults."""
    _, (_, _, urls_file) = _paths()
    if os.path.isfile(urls_file):
        try:
            with open(urls_file, encoding="utf-8") as fh:
                urls = parse_urls(fh.read())
            if urls:
                return urls
        except OSError:
            pass
    return list(config.LINKEDIN_URLS)


def urls_are_custom() -> bool:
    return os.path.isfile(_paths()[1][2])


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
    d, (resume_pdf, resume_meta_file, _) = _paths()
    os.makedirs(d, exist_ok=True)
    tmp = resume_pdf + ".tmp"
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
    os.replace(tmp, resume_pdf)
    meta = {
        "filename": os.path.basename(original_name or "resume.pdf"),
        "pages": pages,
        "bytes": len(data),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(resume_meta_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(f"{k}={v}" for k, v in meta.items()))
    return meta


def get_resume_pdf_path() -> str | None:
    """Path to the profile's uploaded resume, or None when using the bundled one."""
    resume_pdf = _paths()[1][0]
    return resume_pdf if os.path.isfile(resume_pdf) else None


def resume_meta() -> dict | None:
    _, (resume_pdf, meta_path, _) = _paths()
    if not os.path.isfile(resume_pdf):
        return None
    meta: dict[str, str] = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                for line in fh:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        meta[k] = v
        except OSError:
            pass
    meta.setdefault("filename", "resume.pdf")
    meta["path"] = resume_pdf
    return meta


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
def reset(which: str = "all") -> list[str]:
    """Delete the profile's cached files so bundled defaults apply again.

    Returns the list of things reset.
    """
    reset_items: list[str] = []
    _, (resume_pdf, resume_meta_file, urls_file) = _paths()
    targets = {
        "resume": [resume_pdf, resume_meta_file],
        "urls": [urls_file],
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
    custom_urls = urls_are_custom()
    urls = get_search_urls()
    rmeta = resume_meta()
    return {
        "profile": _profiles.current_slug(),
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
