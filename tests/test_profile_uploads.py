"""The profile card on Home and what can be uploaded there (CV, portfolio PDF,
photo): kept in the app's data folder, used only where the portfolio lacks it."""

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from vs import portfolio

TEMPLATE = Path(__file__).resolve().parent.parent / "portfolio-template"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def _pdf(path: Path) -> Path:
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        w.write(f)
    return path


def _folder(tmp_path, monkeypatch) -> tuple[Path, Path]:
    root, up = tmp_path / "pf", tmp_path / "up"
    shutil.copytree(TEMPLATE, root)
    (root / "cv.pdf").unlink(missing_ok=True)
    up.mkdir()
    monkeypatch.setattr(portfolio, "uploads_dir", lambda: up)
    return root, up


def test_an_uploaded_cv_and_photo_fill_what_the_portfolio_lacks(tmp_path, monkeypatch):
    root, up = _folder(tmp_path, monkeypatch)
    before = portfolio.load(root, fmt="folder")
    assert not before.has_cv and before.portrait is None and before.uploaded == ()
    _pdf(up / "cv.pdf")
    (up / "portrait.png").write_bytes(PNG)
    after = portfolio.load(root, fmt="folder")
    assert after.has_cv and after.cv_path == up / "cv.pdf" and after.portrait == up / "portrait.png"
    assert set(after.uploaded) == {"cv", "portrait"}
    assert after.profile_hash != before.profile_hash  # derived data is redone
    assert not any("cv.pdf" in w for w in after.warnings)
    assert not (root / "cv.pdf").exists()  # the portfolio is never written


def test_the_portfolio_always_wins_over_an_upload(tmp_path, monkeypatch):
    root, up = _folder(tmp_path, monkeypatch)
    _pdf(root / "cv.pdf")
    (root / "portrait.png").write_bytes(PNG)
    _pdf(up / "cv.pdf")
    (up / "portrait.png").write_bytes(PNG)
    p = portfolio.load(root, fmt="folder")
    assert p.cv_path == root / "cv.pdf" and p.portrait == root / "portrait.png" and p.uploaded == ()


def test_without_any_portfolio_the_uploaded_pdfs_stand_in(tmp_path, monkeypatch):
    up = tmp_path / "up"
    up.mkdir()
    monkeypatch.setattr(portfolio, "uploads_dir", lambda: up)
    _pdf(up / "portfolio.pdf")
    p = portfolio.load(tmp_path / "missing", fmt="folder")
    assert p.root == up and "portfolio" in p.uploaded


def test_a_photo_path_in_the_portfolio_cant_leave_it(tmp_path):
    (tmp_path / "secret.png").write_bytes(PNG)
    base = tmp_path / "site" / "public"
    base.mkdir(parents=True)
    assert portfolio._inside(base, "../../secret.png") is None
    (base / "me.png").write_bytes(PNG)
    assert portfolio._inside(base, "me.png") == (base / "me.png").resolve()


def test_home_card_uploads_check_the_content_and_can_be_removed(tmp_path, monkeypatch):
    from vs.web import app as webapp
    root, up = _folder(tmp_path, monkeypatch)
    monkeypatch.setattr(webapp, "_portfolio", lambda: portfolio.load(root, fmt="folder"))
    client = TestClient(webapp.app)
    home = client.get("/").text
    card = home.split('id="profile"')[1].split("</section>")[0]
    assert "Your Name" in card and 'action="/profile/upload"' in card and 'value="cv"' in card
    # a renamed image is not a CV
    r = client.post("/profile/upload", data={"kind": "cv"}, files={"file": ("cv.pdf", PNG, "application/pdf")},
                    follow_redirects=False)
    assert r.headers["location"] == "/?profile=wrong-type#profile" and not (up / "cv.pdf").exists()
    assert "the right kind of file" in client.get("/?profile=wrong-type").text
    r = client.post("/profile/upload", data={"kind": "cv"},
                    files={"file": ("mine.pdf", _pdf(tmp_path / "x.pdf").read_bytes(), "application/pdf")}, follow_redirects=False)
    assert r.headers["location"] == "/?profile=saved#profile" and (up / "cv.pdf").exists()
    card = client.get("/").text.split('id="profile"')[1].split("</section>")[0]
    assert "profile-cv" in card and 'value="cv"' in card.split("Added here")[1]
    client.post("/profile/upload", data={"kind": "portrait"}, files={"file": ("me.png", PNG, "image/png")})
    assert client.get("/profile/portrait").content == PNG
    client.post("/profile/remove", data={"kind": "cv"})
    assert not (up / "cv.pdf").exists() and (up / "portrait.png").exists()
    assert client.post("/profile/upload", data={"kind": "nope"}).status_code == 400
