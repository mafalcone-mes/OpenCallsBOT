"""A refresh notices a model left on the CPU, shows its speed, and keeps
only the text of call pages (not their HTML, nor source listings)."""

from datetime import timedelta

import httpx

from vs import db, housekeeping, llm
from vs.fetch.polite import PoliteClient

HTML = "<html><body><nav>menu menu menu</nav><article>" + "<p>Open call for designers.</p>" * 40 + "</article></body></html>"


def _ps(share_by_model: dict):
    def fake_get(url, **kw):
        models = [{"name": m, "size": 1000, "size_vram": int(1000 * s)} for m, s in share_by_model.items()]
        return httpx.Response(200, json={"models": models}, request=httpx.Request("GET", url))
    return fake_get


def test_share_on_the_gpu_is_read_from_ollama(monkeypatch):
    monkeypatch.setattr(httpx, "get", _ps({"qwen:9b": 0.0, "bge-m3:latest": 1.0}))
    assert llm.on_gpu("qwen:9b") == 0.0
    assert llm.on_gpu("bge-m3:latest") == 1.0
    assert llm.on_gpu("not-loaded") is None


def test_a_model_on_the_cpu_is_reloaded_once_then_reported(monkeypatch):
    loads, unloads = [], []
    monkeypatch.setattr(llm, "warm", lambda m=None: loads.append(m))
    monkeypatch.setattr(llm, "unload", lambda m: unloads.append(m))
    monkeypatch.setattr(httpx, "get", _ps({"m": 0.0}))
    warning = llm.check_gpu("m")
    assert loads == ["m", "m"] and unloads == ["m"]
    assert "entirely on the CPU" in warning and llm.gpu_state["warning"] == warning


def test_a_model_on_the_gpu_needs_nothing(monkeypatch):
    loads = []
    monkeypatch.setattr(llm, "warm", lambda m=None: loads.append(m))
    monkeypatch.setattr(llm, "unload", lambda m: (_ for _ in ()).throw(AssertionError("no reload")))
    monkeypatch.setattr(httpx, "get", _ps({"m": 1.0}))
    assert llm.check_gpu("m") is None
    assert loads == ["m"] and "warning" not in llm.gpu_state


def test_live_speed_shows_a_slow_model():
    llm.take_stats()
    llm._recent.clear()
    assert llm.live_speed() is None
    llm._record("extract", 60.0, {"eval_count": 120, "eval_duration": 20e9})
    speed = llm.live_speed()
    assert speed["s_per_call"] == 60.0 and speed["tok_s"] == 6.0 < llm.SLOW_TOK_S
    llm._recent.clear()


def _client(monkeypatch, html=HTML):
    client = PoliteClient()
    client.min_delay = client.max_delay = 0
    monkeypatch.setattr(client, "allowed", lambda url: True)
    hits = []

    def request(method, url, headers=None, **kw):
        hits.append(url)
        return httpx.Response(200, text=html, headers={"content-type": "text/html", "etag": '"v1"'},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(client.client, "request", request)
    return client, hits


def test_a_call_page_is_saved_as_text_and_reused(monkeypatch):
    with db.session() as conn:
        conn.execute("DELETE FROM http_cache")
    client, hits = _client(monkeypatch)
    to_text = lambda html: "TEXT:" + str(len(html))  # noqa: E731
    first = client.get("https://a.example/call", max_age=timedelta(days=7), keep=to_text)
    again = client.get("https://a.example/call", max_age=timedelta(days=7), keep=to_text)
    assert first.text == again.text == f"TEXT:{len(HTML)}"
    assert hits == ["https://a.example/call"]  # the second came from the saved text
    with db.session() as conn:
        row = conn.execute("SELECT body, kind FROM http_cache").fetchone()
    assert (row["body"], row["kind"]) == (first.text, "text")


def test_a_listing_page_is_not_saved(monkeypatch):
    with db.session() as conn:
        conn.execute("DELETE FROM http_cache")
    client, hits = _client(monkeypatch)
    assert client.get("https://a.example/list").text == HTML
    client.get("https://a.example/list")
    assert len(hits) == 2
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


def test_whole_html_copies_from_before_are_dropped_by_housekeeping():
    with db.session() as conn:
        conn.execute("DELETE FROM http_cache")
        conn.execute("INSERT INTO http_cache(url, fetched_at, status, body) VALUES('old-html', ?, 200, ?)", (db.now(), HTML))
        conn.execute("INSERT INTO http_cache(url, fetched_at, status, body, kind) VALUES('text', ?, 200, 'x', 'text')", (db.now(),))
        out = housekeeping.clean(conn, {"housekeeping": {}})
        left = [r[0] for r in conn.execute("SELECT url FROM http_cache")]
    assert left == ["text"] and out["cache_pages"] == 1 and out["cache_bytes"] == len(HTML)
