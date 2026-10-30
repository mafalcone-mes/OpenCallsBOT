"""The home chat: the model only writes the search plan, the app answers from
its data, and the same question gives the same answer."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from vs import chat, db, llm


def _add(conn, key, deadline, typ="residency", region="IT", title=None, text=""):
    conn.execute(
        "INSERT INTO opportunity(dedup_key, canonical_url, title_en, deadline, status, extraction_status, first_seen, type, region, raw_text) "
        "VALUES(?,?,?,?,'open','done',?,?,?,?)", (key, key, title or key, deadline, db.now(), typ, region, text))
    return conn.execute("SELECT id FROM opportunity WHERE dedup_key=?", (key,)).fetchone()[0]


def _reset(conn):
    for t in ("opportunity", "user_state", "match"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM kv WHERE k LIKE 'chatplan:%'")


def test_same_question_same_plan_even_if_the_model_changes_its_mind(monkeypatch):
    calls = []

    def fake_chat_json(system, user, schema, **kw):
        calls.append(kw)
        return {"intent": "search", "types": ["residency"], "region": "IT", "words": [], "topic": ""}

    monkeypatch.setattr(llm, "chat_model", lambda: "m")
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    with db.session() as conn:
        _reset(conn)
        p1, _ = chat.plan_for(conn, "Residenze in Italia?", None)
        monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"intent": "help"})
        p2, _ = chat.plan_for(conn, "  residenze in italia ", None)
    assert p1 == p2 and p1["region"] == "IT"
    assert len(calls) == 1 and calls[0]["temperature"] == 0 and calls[0]["seed"] is not None


def test_a_plan_runs_on_the_data_and_offers_to_loosen_when_empty():
    soon = (date.today() + timedelta(days=10)).isoformat()
    later = (date.today() + timedelta(days=90)).isoformat()
    with db.session() as conn:
        _reset(conn)
        _add(conn, "t:it-soon", soon, text="landscape and paesaggio")
        _add(conn, "t:it-later", later)
        _add(conn, "t:nl", soon, region="EU")
        _add(conn, "t:job", soon, typ="job")
        base = {**chat.EMPTY_PLAN, "intent": "search", "types": ["residency"], "region": "IT"}
        a, _ = chat.answer(conn, "", None, [], plan=base)
        assert {r["dedup_key"] for r in a.rows} == {"t:it-soon", "t:it-later"} and a.title.startswith("2 found")
        assert "Residency" in a.chips and "Italy" in a.chips
        a, _ = chat.answer(conn, "", None, [], plan={**base, "words": ["paesaggio"]})
        assert [r["dedup_key"] for r in a.rows] == ["t:it-soon"]
        a, _ = chat.answer(conn, "", None, [], plan={**base, "within_days": 5})
        assert a.title == "Nothing found" and a.loosen and a.loosen[0][0] == "with any deadline" and a.loosen[0][1] == 2


def test_actions_need_results_shown_and_a_confirmation():
    soon = (date.today() + timedelta(days=10)).isoformat()
    with db.session() as conn:
        _reset(conn)
        ids = [_add(conn, f"t:{i}", soon) for i in range(3)]
        save = {**chat.EMPTY_PLAN, "intent": "action", "action": "save", "targets": [1, 3]}
        a, _ = chat.answer(conn, "", None, [], plan=save)
        assert a.kind == "help" and a.title == "Which ones?"
        a, _ = chat.answer(conn, "", None, ids, plan=save)
        assert a.kind == "confirm" and a.confirm["ids"] == [ids[0], ids[2]]
        n = conn.execute("SELECT COUNT(*) FROM user_state WHERE favourite=1").fetchone()[0]
    assert n == 0, "nothing changes before you confirm"
    assert chat.do(a.confirm) == "Saved 2."
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_state WHERE favourite=1").fetchone()[0] == 2


def test_without_the_model_it_searches_the_words(monkeypatch):
    def down():
        raise llm.LLMError("not running")
    monkeypatch.setattr(llm, "chat_model", down)
    with db.session() as conn:
        _reset(conn)
        plan, note = chat.plan_for(conn, "bandi fotografia Bologna", None)
    assert plan["intent"] == "search" and plan["types"] == ["open_call"] and plan["words"] == ["fotografia", "bologna"]
    assert "isn't available" in note


def test_the_question_decides_what_the_plan_keeps():
    filled = {**chat.EMPTY_PLAN, "intent": "search", "types": ["job", "residency"], "region": "IT", "words": ["salvati"], "within_days": 14}
    plan = chat.ground(dict(filled), "i miei salvati")
    assert plan["status"] == "favourite" and not plan["types"] and not plan["region"] and not plan["words"] and not plan["within_days"]
    plan = chat.ground({**chat.EMPTY_PLAN, "intent": "studios", "country": "DE", "words": ["Berlino"]}, "studi a Berlino")
    assert plan["intent"] == "studios" and plan["country"] == "DE" and plan["words"] == ["Berlin"]
    plan = chat.ground(dict(chat.EMPTY_PLAN), "salva la 1 e la terza", n_shown=5)
    assert plan["action"] == "save" and plan["targets"] == [1, 3]
    plan = chat.ground(dict(chat.EMPTY_PLAN), "scarta tutti, troppo corporate", n_shown=3)
    assert plan["action"] == "dismiss" and plan["targets"] == [1, 2, 3] and plan["reason"] == "too_corporate"
    assert chat._stem("musei") in "museum, design" and chat._stem("museums") in "museum"


def test_home_is_the_chat_and_the_feed_moved():
    from vs.web.app import app
    client = TestClient(app)
    home = client.get("/").text
    assert 'hx-post="/chat/ask"' in home and 'href="/feed"' in home
    assert client.get("/feed").status_code == 200
    r = client.post("/chat/ask", data={"q": "", "plan": '{"intent": "help"}'})
    assert "What I can do" in r.text and 'id="chat-last"' in r.text
