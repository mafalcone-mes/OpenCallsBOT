"""The developer view exists only with VS_DEV=1 (start-dev.bat): nobody else
gets the right-hand bar, the dev lines on cards, or the /dev routes."""

import pytest
from fastapi.testclient import TestClient

from vs import db, devtools, llm
from vs.web import app as web


def _one_call():
    with db.session() as conn:
        conn.execute("DELETE FROM run")
        conn.execute("DELETE FROM match")
        conn.execute("DELETE FROM opportunity")
        conn.execute("DELETE FROM kv WHERE k LIKE 'brief%'")  # briefs other tests left for a reused id
        cur = conn.execute(
            "INSERT INTO opportunity(dedup_key, canonical_url, title_en, type, deadline, status, extraction_status, "
            "first_seen, source_id, raw_text) VALUES('t:dev', 'https://example.org/call', 'A dev call', 'open_call', '', "
            "'open', 'done', ?, 'src-x', 'the page text')", (db.now(),))
        conn.execute("INSERT INTO match(opportunity_id, final, prelim, portfolio_sim, bucket) VALUES(?, 0.7, 0.6, 0.5, 'feed')",
                     (cur.lastrowid,))
        return cur.lastrowid


@pytest.fixture
def dev_on(monkeypatch):
    monkeypatch.setattr(devtools, "DEV", True)
    monkeypatch.setitem(web.templates.env.globals, "dev", True)
    # no Ollama and no graphics card in the tests
    monkeypatch.setattr(devtools, "ollama", lambda: {"host": "http://127.0.0.1:11434", "up": False, "version": None, "loaded": []})
    monkeypatch.setattr(devtools, "nvidia", lambda: None)
    monkeypatch.setattr(devtools, "git", lambda: "branch · abc123 message")


def test_hidden_without_vs_dev():
    oid = _one_call()
    client = TestClient(web.app)
    assert 'id="devbar"' not in client.get("/").text
    assert "dev-card" not in client.get("/feed").text
    for url in ("/dev/panel/system", "/dev/panel/logs", f"/dev/o/{oid}.json", f"/dev/o/{oid}/raw"):
        assert client.get(url).status_code == 404, url
    assert client.post("/dev/do/gpu").status_code == 404


def test_panel_cards_and_routes_with_vs_dev(dev_on):
    oid = _one_call()
    client = TestClient(web.app)
    home = client.get("/").text
    assert 'id="devbar"' in home and "dev-t-system" in home and "dev-t-logs" in home

    feed = client.get("/feed").text
    assert "dev-card" in feed and "src-x" in feed and "final 70" in feed

    detail = client.get(f"/o/{oid}").text
    assert "t:dev" in detail, "This page shows the opportunity's key"

    system = client.get("/dev/panel/system").text
    assert "Not reachable" in system and "No NVIDIA card" in system and "abc123" in system
    assert "Refreshes" in client.get("/dev/panel/logs").text

    data = client.get(f"/dev/o/{oid}.json").json()
    assert data["opportunity"]["source_id"] == "src-x" and "raw_text" not in data["opportunity"]
    assert data["match"]["final"] == 0.7
    assert client.get(f"/dev/o/{oid}/raw").text == "the page text"


def test_actions_answer_with_a_message(dev_on, monkeypatch):
    monkeypatch.setattr(devtools, "start_update", lambda: "Update started.")
    monkeypatch.setattr(llm, "extract_model", lambda: (_ for _ in ()).throw(llm.LLMError("Ollama is not reachable")))
    client = TestClient(web.app)
    assert client.post("/dev/do/update").text == "Update started."
    assert "not reachable" in client.post("/dev/do/gpu").text
    assert "saved pages" in client.post("/dev/do/http-cache").text
    assert client.post("/dev/do/nothing").status_code == 404
