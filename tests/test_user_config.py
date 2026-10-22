"""Tests for user-configurable resume + search URLs (no network, no LLM)."""

from __future__ import annotations

import io
import os

import pytest

from agents import config, user_config


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def build_pdf(text: str = "Jane Jobseeker — senior engineer") -> bytes:
    """Minimal but genuinely valid single-page PDF with selectable text."""
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


@pytest.fixture()
def userdir(tmp_path, monkeypatch):
    """Point all user_config paths at a temp dir (profile-agnostic hook)."""
    d = str(tmp_path / "user_config")
    monkeypatch.setattr(user_config, "_override_dir", d)
    monkeypatch.setattr(user_config, "USER_DIR", d)
    monkeypatch.setattr(user_config, "USER_RESUME_PDF", os.path.join(d, "resume.pdf"))
    monkeypatch.setattr(user_config, "USER_URLS_FILE", os.path.join(d, "search_urls.txt"))
    return d


# ---------------------------------------------------------------------------
# URL parsing / validation
# ---------------------------------------------------------------------------
def test_parse_urls_skips_blanks_and_comments():
    raw = "# comment\n\nhttps://a.com/x  \nhttp://b.com\nnot a url"
    assert user_config.parse_urls(raw) == ["https://a.com/x", "http://b.com", "not a url"]


def test_validate_urls_rejects_non_http():
    with pytest.raises(ValueError):
        user_config.validate_urls(["https://ok.com", "ftp://nope"])
    with pytest.raises(ValueError):
        user_config.validate_urls([])


def test_urls_default_to_bundeded_links(userdir):
    assert user_config.get_search_urls() == list(config.LINKEDIN_URLS)
    assert not user_config.urls_are_custom()


def test_urls_save_overwrites_and_roundtrips(userdir):
    n = user_config.save_urls(["https://one.example/?q=a", "https://two.example/?q=b"])
    assert n == 2
    assert user_config.get_search_urls() == ["https://one.example/?q=a",
                                             "https://two.example/?q=b"]
    assert user_config.urls_are_custom()
    # save again overwrites (does not append)
    user_config.save_urls(["https://three.example/"])
    assert user_config.get_search_urls() == ["https://three.example/"]


def test_urls_save_rejects_bad_and_restores_default(userdir):
    user_config.save_urls(["https://good.example/"])
    with pytest.raises(ValueError):
        user_config.save_urls(["javascript:alert(1)"])
    # the bad save must not have clobbered the good file
    assert user_config.get_search_urls() == ["https://good.example/"]
    user_config.reset("urls")
    assert not user_config.urls_are_custom()
    assert user_config.get_search_urls() == list(config.LINKEDIN_URLS)


# ---------------------------------------------------------------------------
# resume upload
# ---------------------------------------------------------------------------
def test_resume_default_none(userdir):
    assert user_config.get_resume_pdf_path() is None
    assert user_config.resume_meta() is None


def test_resume_rejects_non_pdf(userdir):
    with pytest.raises(ValueError):
        user_config.save_resume_pdf(b"plain text not a pdf", "x.pdf")
    assert not os.path.isdir(userdir) or not os.path.exists(user_config.USER_RESUME_PDF)


def test_resume_rejects_corrupt_pdf(userdir):
    with pytest.raises(ValueError):
        user_config.save_resume_pdf(b"%PDF-1.4\nbroken garbage", "x.pdf")


def test_resume_rejects_oversize(userdir, monkeypatch):
    monkeypatch.setattr(user_config, "MAX_RESUME_BYTES", 10)
    with pytest.raises(ValueError):
        user_config.save_resume_pdf(build_pdf(), "x.pdf")


def test_resume_save_roundtrip_and_reset(userdir):
    meta = user_config.save_resume_pdf(build_pdf("Custom Resume Person"), "my resume.pdf")
    assert meta["pages"] == 1
    assert meta["filename"] == "my resume.pdf"
    assert user_config.get_resume_pdf_path() == user_config.USER_RESUME_PDF

    # resume.get_resume() must now read the uploaded file, not the bundled one
    from agents.tools import resume as resume_mod
    text = resume_mod.get_resume("software")
    assert "Custom Resume Person" in text

    st = user_config.status()
    assert st["resume"]["custom"] is True

    user_config.reset("resume")
    assert user_config.get_resume_pdf_path() is None
    assert user_config.status()["resume"]["custom"] is False


def test_resume_reset_leaves_urls(userdir):
    user_config.save_urls(["https://keep.example/"])
    user_config.save_resume_pdf(build_pdf(), "r.pdf")
    user_config.reset("resume")
    assert user_config.get_search_urls() == ["https://keep.example/"]


def test_reset_unknown_target(userdir):
    with pytest.raises(ValueError):
        user_config.reset("everything")


# ---------------------------------------------------------------------------
# web API
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(userdir):
    from fastapi.testclient import TestClient
    from agents import web
    return TestClient(web.app)


def test_api_config_defaults(client):
    d = client.get("/api/config").json()
    assert d["resume"]["custom"] is False
    assert d["urls"]["custom"] is False
    assert d["urls"]["count"] == len(config.LINKEDIN_URLS)
    assert d["urls"]["urls"] == list(config.LINKEDIN_URLS)


def test_api_urls_save_and_reset(client):
    r = client.post("/api/config/urls", json={"urls": ["https://x.example/?k=1"]})
    assert r.status_code == 200
    assert r.json()["saved"] == 1
    d = client.get("/api/config").json()
    assert d["urls"]["custom"] is True and d["urls"]["count"] == 1

    r = client.post("/api/config/urls", json={"urls": ["mailto:nope"]})
    assert r.status_code == 400
    assert "http" in r.json()["detail"]

    r = client.post("/api/config/reset?which=urls")
    assert r.status_code == 200
    assert client.get("/api/config").json()["urls"]["custom"] is False


def test_api_resume_upload_and_reset(client):
    pdf = build_pdf("Api Uploaded Resume")
    r = client.post("/api/config/resume",
                    files={"file": ("mine.pdf", io.BytesIO(pdf), "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["resume"]["filename"] == "mine.pdf"
    assert client.get("/api/config").json()["resume"]["custom"] is True

    r = client.post("/api/config/resume",
                    files={"file": ("bad.pdf", io.BytesIO(b"not a pdf"), "application/pdf")})
    assert r.status_code == 400

    r = client.post("/api/config/reset?which=resume")
    assert r.status_code == 200
    assert client.get("/api/config").json()["resume"]["custom"] is False


def test_api_reset_bad_which(client):
    assert client.post("/api/config/reset?which=nope").status_code == 422
