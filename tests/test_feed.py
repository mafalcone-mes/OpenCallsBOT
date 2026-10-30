"""Closed calls are hidden, unless you saved them before they closed."""

from datetime import date, timedelta

from vs import db
from vs.report import feed_rows


def _add(conn, key, deadline, status="open"):
    conn.execute(
        "INSERT INTO opportunity(dedup_key, canonical_url, title_en, deadline, status, extraction_status, first_seen) "
        "VALUES(?,?,?,?,?,'done',?)", (key, key, key, deadline, status, db.now()))


def test_closed_calls_hidden_unless_saved():
    future = (date.today() + timedelta(days=20)).isoformat()
    past = (date.today() - timedelta(days=3)).isoformat()
    with db.session() as conn:
        conn.execute("DELETE FROM opportunity")
        conn.execute("DELETE FROM user_state")
        _add(conn, "t:open", future)
        _add(conn, "t:no-deadline", "")
        _add(conn, "t:passed-not-yet-marked", past)          # deadline passed since the last refresh
        _add(conn, "t:expired", past, "expired")
        _add(conn, "t:expired-favourite", past, "expired")
        _add(conn, "t:expired-applied", past, "expired")
        conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES('t:expired-favourite', 1)")
        conn.execute("INSERT INTO user_state(opportunity_key, app_status) VALUES('t:expired-applied', 'submitted')")
        keys = [r["dedup_key"] for r in feed_rows(conn)]
    assert set(keys) == {"t:open", "t:no-deadline", "t:expired-favourite", "t:expired-applied"}
    assert keys[-2:] and set(keys[-2:]) == {"t:expired-favourite", "t:expired-applied"}, "closed saved calls come last"


def _add_typed(conn, key, type_, score, flag="ok"):
    cur = conn.execute(
        "INSERT INTO opportunity(dedup_key, canonical_url, title_en, type, deadline, status, extraction_status, first_seen) "
        "VALUES(?,?,?,?,'','open','done',?)", (key, key, key, type_, db.now()))
    conn.execute("INSERT INTO match(opportunity_id, final, bucket, eligibility_flag) VALUES(?,?,'feed',?)",
                 (cur.lastrowid, score, flag))


def test_feed_tabs_page_through_every_item(monkeypatch):
    """Each kind of opportunity gets its own tab, and 'Show more' reaches every item."""
    from fastapi.testclient import TestClient
    from vs import present
    from vs.web.app import app

    monkeypatch.setattr(present, "FEED_PAGE", 3)
    with db.session() as conn:
        conn.execute("DELETE FROM run")  # no finished refresh: nothing counts as new
        conn.execute("DELETE FROM match")
        conn.execute("DELETE FROM opportunity")
        conn.execute("DELETE FROM user_state")
        for i in range(7):
            _add_typed(conn, f"t:job{i}", "job", 0.9 - i / 100)
        _add_typed(conn, "t:job-bad", "job", 0.99, "likely_ineligible")
        _add_typed(conn, "t:call", "open_call", 0.2)
        _add_typed(conn, "t:new-type", "hackathon", 0.5)  # unknown types land in "Other"
    client = TestClient(app)

    page = client.get("/feed").text
    assert "Open calls" in page and "t:call" in page, "opens on the first tab with something in it"

    page = client.get("/feed?g=jobs").text
    assert "t:job0" in page and "t:job3" not in page and "Show 3 more" in page
    seen = []
    offset = 3
    while True:
        more = client.get(f"/feed/more?g=jobs&offset={offset}").text
        seen += [k for k in [f"t:job{i}" for i in range(7)] + ["t:job-bad"] if f">{k}<" in more]
        if "Show" not in more:
            break
        offset += 3
    assert seen[-1] == "t:job-bad", "likely ineligible comes last, whatever its score"
    assert len(seen) == 5, "the first page had 3 of the 8 jobs; 'Show more' brings the other 5"

    assert "t:new-type" in client.get("/feed?g=other").text


def test_last_refresh_finds_go_on_top_and_have_their_own_page(monkeypatch):
    """What the last finished refresh found is shown first, then a rule, then the rest."""
    from fastapi.testclient import TestClient
    from vs import present
    from vs.web.app import app

    monkeypatch.setattr(present, "FEED_PAGE", 3)
    with db.session() as conn:
        for t in ("match", "opportunity", "user_state", "run"):
            conn.execute(f"DELETE FROM {t}")
        for i in range(5):
            _add_typed(conn, f"t:old{i}", "job", 0.95 - i / 100)
        conn.execute("UPDATE opportunity SET first_seen = '2026-01-01T00:00:00+00:00'")
        conn.execute("INSERT INTO run(started, ended, status) VALUES('2026-02-01T00:00:00+00:00', '2026-02-01T01:00:00+00:00', 'done')")
        for i in range(2):
            _add_typed(conn, f"t:fresh{i}", "job", 0.5)  # lower score, still on top
    client = TestClient(app)

    page = client.get("/feed?g=jobs").text
    assert "New from the last refresh" in page and "+2" in page
    top, rest = page.split("Everything else")
    assert ">t:fresh0<" in top and ">t:fresh1<" in top and ">t:old0<" not in top
    assert ">t:old0<" in rest and ">t:fresh0<" not in rest
    assert "Show 2 more" in rest, "the 5 older ones page on their own: 3 shown, 2 more"
    more = client.get("/feed/more?g=jobs&offset=3&older=1").text
    assert ">t:old3<" in more and ">t:old4<" in more and ">t:fresh0<" not in more and "Show" not in more

    only = client.get("/feed?g=jobs&new=1").text
    assert ">t:fresh0<" in only and ">t:old0<" not in only and "Everything else" not in only


def test_far_scores_sink_under_their_own_divider_and_ties_follow_the_source(monkeypatch):
    from fastapi.testclient import TestClient
    from vs import present
    from vs.web.app import app

    monkeypatch.setattr(present, "FEED_PAGE", 2)
    with db.session() as conn:
        for t in ("run", "match", "opportunity", "user_state"):
            conn.execute(f"DELETE FROM {t}")
        _add_typed(conn, "t:far", "job", 0.30)
        _add_typed(conn, "t:close-bad", "job", 0.70, "likely_ineligible")
        _add_typed(conn, "t:tie-weak-source", "job", 0.651)
        _add_typed(conn, "t:tie-strong-source", "job", 0.649)
        conn.execute("UPDATE match SET tiebreak = 0.5 WHERE opportunity_id = "
                     "(SELECT id FROM opportunity WHERE dedup_key='t:tie-strong-source')")
        keys = [r["dedup_key"] for r in feed_rows(conn)]
    assert keys == ["t:tie-strong-source", "t:tie-weak-source", "t:close-bad", "t:far"]
    client = TestClient(app)
    assert "Far from your work" not in client.get("/feed?g=jobs").text
    more = client.get("/feed/more?g=jobs&offset=2").text
    assert more.index("Far from your work") < more.index("t:far") and more.index("t:close-bad") < more.index("Far from")
    assert more.count("Far from your work") == 1, "only once, where scores cross 40"
    assert "Far from your work" in client.get("/feed/more?g=jobs&offset=3").text, "also when a page starts at the line"


def test_while_a_refresh_runs_each_scored_one_appears_on_top_right_away(monkeypatch):
    from fastapi.testclient import TestClient
    from vs import pipeline
    from vs.web.app import app

    started = db.now()
    monkeypatch.setattr(pipeline, "progress_state", lambda: {"running": True, "kind": "refresh", "started": started,
                                                             "stopping": False, "lines": [], "agents": []})
    with db.session() as conn:
        for t in ("run", "match", "opportunity", "user_state"):
            conn.execute(f"DELETE FROM {t}")
        _add_typed(conn, "t:old-job", "job", 0.9)
        conn.execute("UPDATE opportunity SET first_seen='2000-01-01T00:00:00+00:00'")
        _add_typed(conn, "t:just-scored", "job", 0.7)
        conn.execute("UPDATE opportunity SET first_seen=? WHERE dedup_key='t:just-scored'", ("2999-01-01T00:00:00+00:00",))
        _add(conn, "t:read-not-scored", "")  # read, but the Scorer has not reached it yet
        conn.execute("UPDATE opportunity SET type='job', first_seen=? WHERE dedup_key='t:read-not-scored'",
                     ("2999-01-01T00:00:00+00:00",))
    client = TestClient(app)
    page = client.get("/feed?g=jobs").text
    live = page[page.index('id="live"'):page.index("</section>", page.index('id="live"'))]
    assert "Arriving now" in live and "t:just-scored" in live and "t:old-job" not in live
    assert 'hx-trigger="every 5s"' in live
    assert "t:read-not-scored" not in page, "it appears once it has its score"
    assert page.count(">t:just-scored<") == 1, "not repeated in the list below"
    assert "t:just-scored" in client.get("/feed/live?g=jobs").text


def test_cards_read_one_day_and_show_each_project_once():
    from vs import present
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert present.deadline_parts(tomorrow)[0] == "1 day left"
    assert present.deadline_label(tomorrow).startswith("1 day · ")
    assert present.deadline_parts("", "rolling") == ("Rolling", "")
    chips = present.project_chips([{"angle": "il-valore:photo"}, {"angle": "il-valore:design"}, {"angle": "corpus:art", "why": "w"}])
    assert [c["label"] for c in chips] == ["Il Valore", "Corpus"]
    assert 'aria-pressed="true"' in present.save_button(3, True) and "Saved" in present.save_button(3, True)


def test_similar_to_shows_titles_never_internal_keys():
    from vs.web.app import _similar_titles
    with db.session() as conn:
        conn.execute("DELETE FROM opportunity")
        _add(conn, "https://x.org/starred", "")
        conn.execute("UPDATE opportunity SET title_en='Starred residency' WHERE dedup_key='https://x.org/starred'")
        me = {"dedup_key": "https://x.org/me", "canonical_url": "https://x.org/me", "title_en": "This call",
              "similar_examples": ["opp:https://x.org/me", "opp:https://x.org/starred", "opp:https://x.org/starred", "example:gone-missing"]}
        assert _similar_titles(conn, me) == [("opp:https://x.org/starred", "Starred residency")]


def test_dismiss_leaves_an_undo_line_and_never_echoes_the_raw_reason():
    from fastapi.testclient import TestClient
    from vs.web.app import app
    future = (date.today() + timedelta(days=20)).isoformat()
    with db.session() as conn:
        conn.execute("DELETE FROM opportunity")
        _add(conn, "t:dismiss-me", future)
        oid = conn.execute("SELECT id FROM opportunity WHERE dedup_key='t:dismiss-me'").fetchone()[0]
    client = TestClient(app)
    r = client.post(f"/o/{oid}/dismiss", data={"reason": "not_my_field"}, headers={"HX-Request": "true"})
    assert "Dismissed: Not my field" in r.text and "Undo" in r.text
    r = client.post(f"/o/{oid}/dismiss", data={"reason": "<script>x</script>"}, headers={"HX-Request": "true"})
    assert "<script>" not in r.text and "Dismissed: Other" in r.text
