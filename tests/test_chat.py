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


def test_home_has_the_hero_and_a_question_opens_a_chat():
    from vs.web.app import app
    client = TestClient(app)
    home = client.get("/").text
    hero = home.split('class="hero"')[1].split("</section>")[0]
    assert 'action="/ask"' in hero and 'href="/ask?q=my+saved+ones"' in hero and 'hx-post="/chat/warm"' in hero
    assert 'href="/feed"' in home and 'id="closing-h"' in home  # the sections below the hero
    assert 'id="sb-rail"' in home and 'title="Home"' in home  # the side bar collapses; Oppo leads Home
    r = client.get("/ask", follow_redirects=False)  # no empty chat page
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/ask?q=museums").text
    assert 'ask("museums")' in page and 'hx-post="/chat/ask"' in page and "data-state" not in page
    assert client.get("/feed").status_code == 200
    r = client.post("/chat/ask", data={"q": "", "plan": '{"intent": "help"}'})
    assert "What I can do" in r.text and 'id="chat-last"' in r.text


def test_a_chat_is_saved_and_can_be_continued():
    from vs.web.app import app
    client = TestClient(app)
    r = client.post("/chat/ask", data={"q": "help me", "plan": '{"intent": "help"}'})
    cid = int(r.headers["HX-Push-Url"].rsplit("/", 1)[1])
    assert 'id="chat-id" value="%d"' % cid in r.text and 'id="sb-chats"' in r.text
    client.post("/chat/ask", data={"q": "and again", "plan": '{"intent": "help"}', "chat_id": str(cid)})
    page = client.get(f"/chat/{cid}").text
    assert "help me" in page and "and again" in page and 'id="chat-form"' in page
    assert f'href="/chat/{cid}"' in client.get("/").text  # in the side bar


def test_a_confirmed_action_is_not_offered_again_when_the_chat_reopens():
    from vs import chatlog
    with db.session() as conn:
        cid = chatlog.create(conn, "save 1")
        chatlog.add_turn(conn, cid, "save 1", '<div class="chat-a"><div class="chat-confirm"><button>Yes, do it</button></div></div>', "")
        chatlog.settle(conn, cid, 0, '<p class="chat-done">Cancelled: nothing changed.</p>')
        html = chatlog.get(conn, cid)["turns"][0]["html"]
    assert "chat-confirm" not in html and "Cancelled" in html


def test_todo_board_moves_cards_in_bulk_and_restores_deleted_calls():
    from vs import todo
    from vs.web.app import app
    soon = (date.today() + timedelta(days=3)).isoformat()
    with db.session() as conn:
        _reset(conn)
        conn.execute("DELETE FROM todo")
        conn.execute("DELETE FROM kv WHERE k IN ('todo_state', 'deadline_alert_day')")
        oid = _add(conn, "k-todo", soon, title="Photo residency")
        conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES('k-todo', 1)")
        todo.add_own(conn, "Update the CV", "portfolio")
        own = conn.execute("SELECT id FROM todo").fetchone()[0]
        b = todo.board(conn)
    assert [c["key"] for c in b["todo"]] == [f"call:{oid}", f"own:{own}"] and b["todo"][0]["sub"] == "Next: check what they ask for"
    client = TestClient(app)
    # what closes within a week is on Today (no pop-up any more)
    today = client.get("/").text
    assert "Photo residency" in today.split('id="closing-h"')[1].split("</section>")[0]
    assert 'id="deadline-alert"' not in client.get("/feed").text
    assert client.get("/tracker", follow_redirects=False).headers["location"] == "/todo"
    # both selected, moved to In progress at once: the call's application status follows
    client.post("/todo/act", data={"keys": [f"call:{oid}", f"own:{own}"], "action": "progress"})
    with db.session() as conn:
        b = todo.board(conn)
        assert {c["key"] for c in b["progress"]} == {f"call:{oid}", f"own:{own}"}
        assert conn.execute("SELECT app_status FROM user_state WHERE opportunity_key='k-todo'").fetchone()[0] == "preparing"
    client.post("/todo/act", data={"keys": f"call:{oid}", "action": "done"})
    client.post("/todo/act", data={"keys": f"call:{oid}", "action": "rejected"})
    client.post("/todo/act", data={"keys": f"call:{oid}", "action": "deleted"})
    with db.session() as conn:
        b = todo.board(conn)
        assert [c["key"] for c in b["deleted"]] == [f"call:{oid}"] and not b["done"]
        assert conn.execute("SELECT favourite, app_status FROM user_state WHERE opportunity_key='k-todo'").fetchone()[:] == (0, None)
    client.post("/todo/act", data={"keys": f"call:{oid}", "action": "restore"})
    with db.session() as conn:
        b = todo.board(conn)
        assert b["done"][0]["outcome"] == "rejected" and not b["deleted"] and todo.open_count(conn) == 1


def test_a_follow_up_keeps_what_the_earlier_question_asked():
    plan = chat.ground({**chat.EMPTY_PLAN, "intent": "search", "types": ["residency"], "country": "DE"},
                       "e in Germania?", context="residenze in Italia")
    assert plan["types"] == ["residency"] and plan["country"] == "DE"
    # without the earlier question, "residency" has nothing to stand on
    assert chat.ground({**chat.EMPTY_PLAN, "intent": "search", "types": ["residency"]}, "e in Germania?")["types"] == []
    # what the app adds by itself comes from the new question only
    assert chat.ground({**chat.EMPTY_PLAN, "intent": "search"}, "jobs", context="my saved ones")["status"] == ""


def test_the_reply_is_written_from_the_facts_and_saved(monkeypatch):
    from vs.web.app import app
    seen = {}

    def fake(system, user, schema, **kw):
        seen["user"] = user
        return {"reply": "Two fit; #1 closes first."}

    client = TestClient(app)
    r = client.post("/chat/ask", data={"q": "residencies", "plan": '{"intent": "help"}'})
    cid = int(r.headers["HX-Push-Url"].rsplit("/", 1)[1])
    assert f"/chat/{cid}/reply/0" in r.text
    monkeypatch.setattr(llm, "chat_json", fake)
    out = client.get(f"/chat/{cid}/reply/0").text
    assert "#1 closes first" in out and "FACTS" in seen["user"] and "residencies" in seen["user"]
    page = client.get(f"/chat/{cid}").text
    assert "#1 closes first" in page and f"/chat/{cid}/reply/0" not in page  # saved in place of the dots


def test_todo_board_copes_with_calls_without_a_deadline():
    from vs import todo
    with db.session() as conn:
        _reset(conn)
        conn.execute("INSERT INTO opportunity(dedup_key, canonical_url, title_en, deadline, status, extraction_status, first_seen) "
                     "VALUES('k-nodl', 'k-nodl', 'No deadline call', NULL, 'open', 'done', ?)", (db.now(),))
        conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES('k-nodl', 1)")
        conn.execute("INSERT INTO user_state(opportunity_key, app_status) VALUES('k-x', 'preparing')")
        b = todo.board(conn)
    assert any(c["title"] == "No deadline call" and c["due"] == "" for c in b["todo"])


def test_a_question_naming_a_site_offers_to_check_it_and_reads_only_that_site(monkeypatch):
    import time as _time
    from vs import pipeline
    from vs.web.app import app
    sources = {"accurat": {"adapter": "html_list", "url": "https://www.accurat.it/jobs", "enabled": True},
               "fablab-bcn": {"adapter": "html_list", "url": "https://fablabbcn.org/jobs", "name": "Fab Lab Barcelona (jobs)"},
               "resartis": {"adapter": "html_list", "url": "https://resartis.org/open-calls/"}}
    monkeypatch.setattr(chat, "prefs", lambda: {"sources": sources, "watchlist": []})
    import vs.studios
    monkeypatch.setattr(vs.studios, "load", lambda: [{"name": "Studio Blanco", "jobs_url": "https://blanco.example/jobs"}])
    assert [s["id"] for s in chat.named_sites("Accurat sta cercando qualcuno?")] == ["accurat"]
    assert [s["id"] for s in chat.named_sites("il fablab ha posizioni aperte?")] == ["fablab-bcn"]
    assert [s["kind"] for s in chat.named_sites("is studio blanco hiring")] == ["studio"]
    assert chat.named_sites("any workshops in Barcelona?") == [] and chat.named_sites("open calls") == []

    with db.session() as conn:
        _reset(conn)
    client = TestClient(app)
    r = client.post("/chat/ask", data={"q": "Accurat sta cercando qualcuno?",
                                       "plan": '{"intent": "search", "words": ["Accurat"]}'})
    assert "Check now" in r.text and "/chat/check" in r.text
    cid = int(r.headers["HX-Push-Url"].rsplit("/", 1)[1])

    ran = {}

    def fake_refresh(**kw):
        ran.update(kw)
        with db.session() as conn:
            _add(conn, "k-acc", "", title="Junior designer at Accurat")
            conn.execute("UPDATE opportunity SET source_id='accurat' WHERE dedup_key='k-acc'")
        return {}
    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    started = client.post("/chat/check", data={"ids": "accurat|not-a-source", "chat_id": cid, "turn": 0}).text
    job_id = started.split("/chat/check/")[1].split("?")[0]
    for _ in range(50):
        if pipeline.check_state(job_id)["state"] == "done":
            break
        _time.sleep(0.1)
    assert ran["only"] == ["accurat"] and ran["force"] and not ran["extra_jobs"]
    done = client.get(f"/chat/check/{job_id}?chat_id={cid}&turn=0").text
    assert "Checked accurat: 1 open" in done and "Junior designer at Accurat" in done
    assert "Junior designer at Accurat" in client.get(f"/chat/{cid}").text  # saved with the chat


def test_todo_lists_only_the_documents_a_call_asks_for_and_groups_by_what_is_missing():
    from vs import todo
    from vs.web.app import app
    soon = (date.today() + timedelta(days=9)).isoformat()
    docs = [{"kind": "portfolio", "details": "max 10 pages PDF", "required": True},
            {"kind": "motivation_letter", "details": "1 page", "required": True},
            {"kind": "cv", "details": "", "required": False}]
    with db.session() as conn:
        _reset(conn)
        conn.execute("DELETE FROM todo")
        conn.execute("DELETE FROM kv WHERE k IN ('todo_state', 'todo_docs', 'todo_notes') OR k LIKE 'brief:%'")
        both = _add(conn, "k-both", soon, title="Asks letter and portfolio")
        only_cv = _add(conn, "k-cv", soon, title="Asks only a CV")
        conn.execute("UPDATE opportunity SET required_docs=? WHERE id=?", (db.dumps(docs), both))
        conn.execute("UPDATE opportunity SET required_docs=? WHERE id=?", (db.dumps([{"kind": "cv", "details": "", "required": True}]), only_cv))
        for k in ("k-both", "k-cv"):
            conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES(?, 1)", (k,))
        b = todo.board(conn)
        cards = {c["oid"]: c for c in b["todo"]}
        assert [d["kind"] for d in cards[both]["docs"]] == ["portfolio", "motivation_letter", "cv"]
        assert [d["kind"] for d in cards[only_cv]["docs"]] == ["cv"]  # never asks for what the call doesn't
        assert cards[both]["group"] == "both" and cards[only_cv]["group"] == "other"
        assert cards[both]["sub"] == "Next: prepare the brief and portfolio PDF (and 1 more)"
        # a portfolio PDF from the brief ticks the portfolio; the letter is still missing
        db.kv_set(conn, f"brief:{both}", {"selected": [], "portfolio_pdf": {"path": "x.pdf"}})
        c = todo.find(todo.board(conn), f"call:{both}")
        assert c["group"] == "letter" and c["sub"] == "Next: write the motivation letter" and c["docs"][0]["auto"]
    client = TestClient(app)
    out = client.post("/todo/doc", data={"oid": both, "doc": "motivation_letter", "on": 1}).text
    assert "Ready: send the application" in out  # the CV is optional
    client.post("/todo/doc", data={"oid": both, "doc": "portfolio", "on": 0})  # untick the automatic tick
    client.post("/todo/note", data={"key": f"call:{both}", "text": "ask Marco for the reference"})
    client.post("/todo/add", data={"text": "Print the CV", "kind": "call", "due": soon, "oid": str(only_cv)})
    with db.session() as conn:
        b = todo.board(conn)
        c = todo.find(b, f"call:{both}")
        assert c["group"] == "portfolio" and c["note"] == "ask Marco for the reference"
        own = next(x for x in b["todo"] if x["own"])
        assert own["due"] == soon and own["sub"] == "For Asks only a CV"
        todo.edit_own(conn, own["own"]["id"], "Print two CVs", "call", "", "")
        assert todo.find(todo.board(conn), own["key"])["title"] == "Print two CVs"
    page = client.get("/todo").text
    assert "Missing portfolio" in page and "Missing other documents" in page and "max 10 pages PDF" in page


def test_clearing_the_feed_hides_what_is_there_keeps_saved_ones_and_can_be_undone():
    from vs.web.app import app
    later = (date.today() + timedelta(days=40)).isoformat()
    with db.session() as conn:
        _reset(conn)
        db.kv_set(conn, "feed_cleared_at", "")
        _add(conn, "k-old", later, title="Old residency")
        _add(conn, "k-kept", later, title="Saved residency")
        conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES('k-kept', 1)")
    client = TestClient(app)
    assert "Old residency" in client.get("/feed?g=residencies").text
    client.post("/feed/clear")
    page = client.get("/feed?g=residencies").text
    assert "Old residency" not in page and "Saved residency" in page and "Show everything again" in page
    with db.session() as conn:  # found by a later refresh: shown
        conn.execute("UPDATE opportunity SET first_seen = '2999-01-01T00:00:00+00:00' WHERE dedup_key = 'k-old'")
    assert "Old residency" in client.get("/feed?g=residencies").text
    with db.session() as conn:
        conn.execute("UPDATE opportunity SET first_seen = '2020-01-01T00:00:00+00:00' WHERE dedup_key = 'k-old'")
    client.post("/feed/clear", data={"undo": "1"})
    assert "Old residency" in client.get("/feed?g=residencies").text


def test_progress_says_interrupted_not_finished_after_the_app_restarted_mid_refresh(monkeypatch):
    from vs import pipeline
    from vs.web.app import app
    monkeypatch.setitem(pipeline._state, "lines", [])  # a fresh app: nothing ran in this process
    monkeypatch.setitem(pipeline._state, "running", False)
    with db.session() as conn:
        conn.execute("DELETE FROM run WHERE status = 'running'")
        conn.execute("INSERT INTO run(started, status) VALUES('2020-01-01T10:00:00+00:00', 'running')")
    page = TestClient(app).get("/progress").text
    assert "was interrupted" in page and "Finished" not in page
    with db.session() as conn:
        conn.execute("DELETE FROM run WHERE status = 'running'")
    assert "Finished" in TestClient(app).get("/progress").text


def test_the_ask_page_loads_the_model_while_you_type(monkeypatch):
    from fastapi.testclient import TestClient
    from vs import pipeline
    from vs.web.app import app
    warmed = []
    monkeypatch.setattr(llm, "warm", lambda *a, **k: warmed.append(1))
    client = TestClient(app)
    assert 'hx-post="/chat/warm"' in client.get("/").text
    assert client.post("/chat/warm").status_code == 204
    import time
    for _ in range(50):
        if warmed:
            break
        time.sleep(0.01)
    assert warmed
    # not during a refresh: its agents decide what is in video memory
    warmed.clear()
    monkeypatch.setitem(pipeline._state, "running", True)
    assert client.post("/chat/warm").status_code == 204
    time.sleep(0.05)
    assert not warmed


def test_a_list_named_in_the_question_is_shown_not_changed():
    # The model may call "my saved ones" an action or ask for help: no action
    # word is in it, so it is a search of the saved list.
    acted = chat.ground({**chat.EMPTY_PLAN, "intent": "action", "action": "save", "status": "favourite"}, "my saved ones")
    assert acted["intent"] == "search" and acted["action"] == "" and acted["status"] == "favourite"
    helped = chat.ground({**chat.EMPTY_PLAN, "intent": "help", "status": "favourite"}, "my saved ones", context="x")
    assert helped["intent"] == "search" and helped["status"] == "favourite"
    # a real action still is one
    saved = chat.ground(dict(chat.EMPTY_PLAN), "save 1 and 3", n_shown=5)
    assert saved["intent"] == "action" and saved["action"] == "save" and saved["targets"] == [1, 3]


def test_a_question_from_home_waits_for_htmx_before_it_is_sent():
    # Asked before htmx has wired the form, it showed on screen but was never sent.
    from vs.web.app import app
    page = TestClient(app).get("/ask?q=museums").text
    script = page.split('ask("museums")')[0].rsplit("<script>", 1)[1]
    assert "htmx:load" in script and "DOMContentLoaded" not in script
