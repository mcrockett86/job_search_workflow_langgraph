"""Tests for the heuristic summarizer."""

from __future__ import annotations

from agents.summary import guess_title_company, preview_text, summarize


SAMPLE = """Skip to main content
Sign in
Acme Corp logo
Senior Machine Learning Engineer at Acme Corp
Acme Corp is building the future of logistics automation. We are looking for an experienced ML engineer to design, train and deploy production models that optimize delivery routing across 40 cities.
You will own the full lifecycle: data pipelines in Python, model training on GPU clusters, and low-latency serving behind a REST API.
Cookie Policy
Privacy Policy
[LinkedIn](https://linkedin.com) · Terms · Help
"""


def test_summarize_picks_prose_and_drops_boilerplate():
    s = summarize(SAMPLE)
    assert "logistics automation" in s
    assert "Cookie Policy" not in s
    assert "Sign in" not in s
    assert len(s) <= 420


def test_summarize_empty_inputs():
    assert summarize(None) == ""
    assert summarize("") == ""


def test_summarize_truncates_long_text():
    text = "word " * 2000
    s = summarize(text)
    assert len(s) <= 421
    assert s.endswith("…")


def test_preview_strips_links_and_boilerplate():
    p = preview_text(SAMPLE)
    assert "Senior Machine Learning Engineer" in p
    assert "linkedin.com" not in p
    assert "Skip to main content" not in p


def test_preview_trims_to_limit():
    text = "\n".join(f"line {i} with plenty of prose content here" for i in range(300))
    p = preview_text(text)
    assert len(p) <= 2201


def test_guess_title_company_at_form():
    title, company = guess_title_company("Senior Machine Learning Engineer at Acme Corp\nmore text")
    assert title == "Senior Machine Learning Engineer"
    assert company == "Acme Corp"


def test_guess_title_company_colon_form():
    title, company = guess_title_company("Globex: Staff Data Scientist\nblah")
    assert title == "Staff Data Scientist"
    assert company == "Globex"


def test_guess_title_company_no_match():
    assert guess_title_company("just some text") == (None, None)
    assert guess_title_company(None) == (None, None)
