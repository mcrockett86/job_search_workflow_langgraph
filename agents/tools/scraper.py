"""Scrape a URL and extract plain-text content."""

from __future__ import annotations

import logging
import requests
from bs4 import BeautifulSoup
import html2text


logger = logging.getLogger(__name__)

# Tags whose entire content should be stripped from the HTML before conversion.
_EXCLUDED_TAGS: tuple[str, ...] = ("footer", "nav", "a", "script")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Ubuntu; Firefox/41.0)",
}

_TIMEOUT_SECONDS = 10


async def scraper_extract_html_from_url(url: str) -> str | None:
    """Fetch *url*, strip unwanted tags, and return plain-text content.

    Returns ``None`` when the request fails or returns empty content.
    """
    try:
        response = requests.get(
            url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        # Remove all excluded tag elements from the DOM tree.
        for tag_name in _EXCLUDED_TAGS:
            for element in soup.find_all(tag_name):  # type: ignore[arg-type]
                element.extract()

        text_content = html2text.html2text(str(soup))
        return text_content if text_content.strip() else None

    except requests.exceptions.RequestException as exc:
        logger.error("Failed to fetch %s — %s", url, exc)
        return None