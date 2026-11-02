"""The beta's limits (vs/beta.py) and the Welcome page (vs/onboarding.py)."""

import tomllib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from vs import beta, build_info, config, onboarding

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Keep the stored times in a temporary folder, never the registry."""
    monkeypatch.setattr(beta, "_data_file", lambda: tmp_path / "data" / "beta.json")
    monkeypatch.setattr(beta, "_outside_file", lambda: tmp_path / "outside" / "beta.json")
    monkeypatch.setattr(beta.sys, "platform", "darwin")
    return tmp_path


def test_from_the_code_nothing_is_locked():
    assert build_info.BETA is False
    assert beta.status() == {"active": False, "over": ""}
    assert beta.locked() == ""


def test_five_days_from_the_first_opening():
    st = beta.evaluate(T0, T0 + timedelta(days=4, hours=23), "", False)
    assert not st["over"] and st["days_left"] == 0 and st["hours_left"] == 1
    assert beta.evaluate(T0, T0 + timedelta(days=5), "", False)["over"] == "time"


def test_build_cap_and_switch():
    # opened 28 days after the build: the 30-day cap ends it after 2 days, not 5
    late = T0 + timedelta(days=28)
    assert beta.evaluate(late, late + timedelta(days=3), T0.date().isoformat(), False)["over"] == "time"
    assert beta.evaluate(T0, T0, "", True)["over"] == "off"


def test_first_opening_survives_a_deleted_data_folder(stores):
    first, _ = beta.remember(now=T0)
    (stores / "data" / "beta.json").unlink()
    again, _ = beta.remember(now=T0 + timedelta(days=2))
    assert first == again == T0


def test_clock_set_back_does_not_help(stores):
    beta.remember(now=T0 + timedelta(days=4))
    _, current = beta.remember(now=T0)
    assert current == T0 + timedelta(days=4)
    _, current = beta.remember(now=T0, web=T0 + timedelta(days=6))
    assert current == T0 + timedelta(days=6)


def test_switch_row_in_the_sheet():
    assert beta.switch_in([{"kind": "beta", "name": "beta", "enabled": "FALSE"}])
    assert not beta.switch_in([{"kind": "beta", "name": "beta", "enabled": "TRUE"}])
    assert not beta.switch_in([{"kind": "source", "id": "x", "enabled": "false"}])


def test_beta_over_page(monkeypatch, stores):
    from vs.web.app import app
    monkeypatch.setattr(build_info, "BETA", True)
    monkeypatch.setattr(build_info, "FEEDBACK_URL", "https://example.org/form")
    monkeypatch.setattr(beta, "status", lambda force=False: {"active": True, "over": "time"})
    c = TestClient(app)
    r = c.get("/feed", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/beta-over"
    page = c.get("/beta-over")
    assert "The beta has ended" in page.text and "https://example.org/form" in page.text
    assert c.get("/static/app.css").status_code == 200


def test_write_prefs_from_the_example(tmp_path):
    path = onboarding.write_prefs({"roles": "graphic designer, grafico", "avoid": "senior", "countries": "Italy",
                                   "citizenships": "it", "languages": "it, en", "seniority": "junior",
                                   "open_to": ["residencies", "not a choice"]}, tmp_path / "preferences.toml", gb=8)
    with open(path, "rb") as f:
        p = tomllib.load(f)
    assert p["portfolio"]["format"] == "pdf" and p["portfolio"]["dir"].endswith("profile")
    assert p["taste"]["keywords_plus"] == ["graphic designer", "grafico"] == p["search"]["queries"]
    assert p["search"]["countries"] == ["italy"] and p["me"]["citizenships"] == ["IT"]
    assert p["me"]["languages"] == {"it": "fluent", "en": "fluent"} and p["me"]["open_to"] == ["residencies"]
    assert p["llm"]["model"] == "qwen3:4b"
    assert p["sheet"]["url"]  # the shared sources come with the example


def test_no_preferences_opens_the_welcome_page(monkeypatch, tmp_path):
    from vs.web.app import app
    monkeypatch.setattr(config, "PREFS_PATH", tmp_path / "preferences.toml")
    monkeypatch.setattr(onboarding, "ollama_running", lambda: False)
    monkeypatch.setattr(onboarding, "start_ollama", lambda: None)
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    page = c.get("/setup")
    assert "Welcome" in page.text and "Download Ollama" in page.text
    r = c.post("/setup", data={"roles": "photographer"}, follow_redirects=False)
    assert r.headers["location"] == "/setup?error=no-portfolio"
    assert not config.PREFS_PATH.exists()


def test_every_beta_build_is_locked(tmp_path, monkeypatch):
    """scripts/build_beta.py always switches the 5-day lock on: no option leaves it off."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_beta", config.RESOURCE_DIR / "scripts" / "build_beta.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    monkeypatch.setattr(build, "INFO", tmp_path / "build_info.py")
    build.write_info("https://example.org/form")
    info = {}
    exec((tmp_path / "build_info.py").read_text(encoding="utf-8"), info)
    assert info["BETA"] is True and info["BUILD_DATE"]
    assert beta.DAYS == 5
