import sqlite3

import numpy as np

from vs import db, embed, extract, llm, pipeline
from vs.fetch import RawItem


def _items(sid: str, n: int) -> list[RawItem]:
    return [RawItem(url=f"https://{sid}.example/{i}", title=f"{sid} {i}", source_id=sid) for i in range(n)]


def test_not_a_call_needs_only_the_short_answer(monkeypatch):
    calls = []

    def fake_chat(system, user, schema, **kw):
        calls.append(set(schema["properties"]))
        return {"is_opportunity": False, "type": "other"}
    monkeypatch.setattr(llm, "chat_json", fake_chat)
    monkeypatch.setattr(llm, "extract_model", lambda: "m")
    data = extract.extract("An interview with a designer about her new book.", title="Interview")
    assert data["is_opportunity"] is False
    assert calls == [set(extract.FIRST_FIELDS)]


def test_a_call_gets_every_field_with_the_same_messages(monkeypatch):
    seen = []

    def fake_chat(system, user, schema, **kw):
        seen.append((system, user))
        return {k: "" for k in schema["properties"]} | ({"is_opportunity": True} if "is_opportunity" in schema["properties"] else {})
    monkeypatch.setattr(llm, "chat_json", fake_chat)
    monkeypatch.setattr(llm, "extract_model", lambda: "m")
    data = extract.extract("Open call for designers. Deadline 1 December 2026.", title="Open call")
    assert set(data) == set(extract.SCHEMA["required"])
    assert len(seen) == 2 and seen[0] == seen[1]  # same prompt: Ollama reuses what it already read


def test_extract_model_falls_back_to_the_chat_model(monkeypatch):
    monkeypatch.setattr(llm, "_cfg", lambda: {"model": "big:9b"})
    monkeypatch.setattr(llm, "available_models", lambda: ["big:9b"])
    assert llm.extract_model() == "big:9b"
    monkeypatch.setattr(llm, "_cfg", lambda: {"model": "big:9b", "extract_model": "small:4b"})
    monkeypatch.setattr(llm, "available_models", lambda: ["big:9b", "small:4b"])
    assert llm.extract_model() == "small:4b"


def test_embeddings_are_committed_before_the_next_model_call(monkeypatch):
    monkeypatch.setattr(llm, "embed", lambda texts, model=None: [np.ones(4, dtype=np.float32) for _ in texts])
    conn = db.connect()
    embed.ensure(conn, "test", {"k1": "some text"}, "m")
    # Another part of the app can write right away (it would wait for the lock otherwise).
    other = sqlite3.connect(db.DB_PATH, timeout=0.5)
    other.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('speed-test', '1')")
    other.commit()
    other.close()
    conn.close()


def test_an_answer_cut_off_by_the_cap_is_asked_again_without_it(monkeypatch):
    sent = []

    class Resp:
        status_code = 200
        text = ""

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def fake_post(url, json, timeout):
        sent.append(dict(json["options"]))
        if "num_predict" in json["options"]:
            return Resp({"done_reason": "length", "message": {"content": '{"is_opportunity": true, "deadline_evidence": "Apply by'}})
        return Resp({"done_reason": "stop", "message": {"content": '{"is_opportunity": true}'}})
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.chat_json("s", "u", {}, model="m", max_tokens=10) == {"is_opportunity": True}
    assert "num_predict" in sent[0] and "num_predict" not in sent[1]


def test_saving_the_log_never_waits_for_the_callers_own_write_lock(monkeypatch):
    import time
    conn = db.connect()
    run_id = conn.execute("INSERT INTO run(started, status) VALUES(?, 'running')", (db.now(),)).lastrowid
    conn.commit()
    monkeypatch.setitem(pipeline._state, "kind", "refresh")
    monkeypatch.setitem(pipeline._state, "running", True)
    monkeypatch.setitem(pipeline._state, "run_id", run_id)
    monkeypatch.setitem(pipeline._state, "lines", [])
    monkeypatch.setitem(pipeline._flushed, "at", 0.0)
    conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('uncommitted', '1')")  # holds the write lock
    started = time.monotonic()
    pipeline._say("a page was read")
    assert time.monotonic() - started < 5
    conn.rollback()
    conn.close()


def test_sessions_reuse_an_open_connection_instead_of_opening_the_file_again():
    with db.session() as a:
        first = id(a)
        with db.session() as b:  # nested: needs its own connection
            assert id(b) != first
    with db.session() as c:
        assert id(c) in (first, id(b))
    try:
        with db.session() as d:
            d.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('pool-test', '1')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.session() as e:
        assert e.execute("SELECT v FROM kv WHERE k='pool-test'").fetchone() is None
        assert id(e) != id(d)  # a session that failed is closed, not reused
