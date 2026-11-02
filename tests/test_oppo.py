"""Oppo, the mascot: drawings dropped in static/oppo/ replace the placeholder."""

from vs.web import oppo as oppo_mod


def test_no_drawing_gives_the_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(oppo_mod, "DIR", tmp_path)
    out = str(oppo_mod.oppo("searching", 32))
    assert 'class="oppo oppo-searching"' in out and 'width="32"' in out
    assert 'aria-hidden="true"' in out and "var(--oppo)" in out


def test_a_missing_pose_uses_the_default_drawing(tmp_path, monkeypatch):
    monkeypatch.setattr(oppo_mod, "DIR", tmp_path)
    (tmp_path / "default.svg").write_text('<?xml version="1.0"?>\n<svg width="500" height="500" viewBox="0 0 96 96"><circle r="9"/></svg>')
    (tmp_path / "found.svg").write_text('<svg viewBox="0 0 96 96"><rect width="9" height="9"/></svg>')
    empty = str(oppo_mod.oppo("empty", 96, label="Oppo"))
    assert "<?xml" not in empty and 'width="500"' not in empty and "<circle" in empty
    assert 'role="img" aria-label="Oppo"' in empty
    found = str(oppo_mod.oppo("found"))
    assert "<rect" in found and 'width="9"' in found  # only the <svg> tag's own size is replaced


def test_the_tab_icon_is_oppo():
    from fastapi.testclient import TestClient

    from vs.web.app import app
    r = TestClient(app).get("/favicon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text
