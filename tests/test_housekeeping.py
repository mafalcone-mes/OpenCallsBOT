"""Housekeeping: only what you saved or applied to is kept once it is over;
removed calls keep a trace so their page is never read again."""

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from vs import db, housekeeping, pipeline
from vs.fetch import RawItem

PREFS = {"housekeeping": {}, "sources": {"listing": {"adapter": "html_list"}, "search": {"adapter": "jobspy"}}}


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _reset(conn):
    for t in ("opportunity", "user_state", "match", "embedding", "http_cache"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM kv WHERE k IN ('cleanup_log', 'recap_month', 'recap_done_at', 'recap_reviewed', 'recap_offer_day', 'deadline_alert_day')")


def _add(conn, key, *, status="open", deadline="", score=None, first_seen=None, last_seen=None, source="listing", missed=0):
    conn.execute(
        "INSERT INTO opportunity(dedup_key, canonical_url, source_id, title_en, deadline, status, extraction_status, first_seen, last_seen, "
        "raw_text, summary_en, content_hash, missed_runs) VALUES(?,?,?,?,?,?, 'done', ?,?,?,?,?,?)",
        (key, key, source, key, deadline, status, first_seen or db.now(), last_seen or db.now(), "long text " * 50, "summary", "h-" + key, missed))
    oid = conn.execute("SELECT id FROM opportunity WHERE dedup_key=?", (key,)).fetchone()[0]
    if score is not None:
        conn.execute("INSERT INTO match(opportunity_id, final) VALUES(?, ?)", (oid, score))
    conn.execute("INSERT INTO embedding(kind, key, text_hash, model_id, vec) VALUES('opportunity', ?, 'x', 'm', x'00')", (key,))
    return oid


def _status(conn, key):
    return conn.execute("SELECT status FROM opportunity WHERE dedup_key=?", (key,)).fetchone()[0]


def test_only_saved_or_applied_calls_are_kept_once_over():
    past = (date.today() - timedelta(days=3)).isoformat()
    with db.session() as conn:
        _reset(conn)
        _add(conn, "closed", status="expired", deadline=past)
        _add(conn, "closed-saved", status="expired", deadline=past)
        _add(conn, "closed-applied", status="expired", deadline=past)
        _add(conn, "not-a-call", status="not_opportunity")
        _add(conn, "far-old", score=0.2, first_seen=_ago(30))
        _add(conn, "far-new", score=0.2, first_seen=_ago(5))
        _add(conn, "far-saved", score=0.2, first_seen=_ago(30))
        _add(conn, "close", score=0.7, first_seen=_ago(30))
        conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES('closed-saved', 1), ('far-saved', 1)")
        conn.execute("INSERT INTO user_state(opportunity_key, app_status) VALUES('closed-applied', 'rejected')")
        dry = housekeeping.clean(conn, PREFS, dry_run=True)
        assert _status(conn, "closed") == "expired"  # a dry run changes nothing
        out = housekeeping.clean(conn, PREFS)
        assert dry["removed"] == out["removed"] == 3 and out["far"] == 1
        assert {k: _status(conn, k) for k in ("closed", "not-a-call", "far-old")} == dict.fromkeys(("closed", "not-a-call", "far-old"), "removed")
        assert {k: _status(conn, k) for k in ("closed-saved", "closed-applied", "far-new", "far-saved", "close")} == {
            "closed-saved": "expired", "closed-applied": "expired", "far-new": "open", "far-saved": "open", "close": "open"}
        gone = conn.execute("SELECT raw_text, summary_en, title_en, content_hash FROM opportunity WHERE dedup_key='closed'").fetchone()
        assert gone[0] is None and gone[1] is None and gone[2] == "closed" and gone[3] == "h-closed"
        assert not conn.execute("SELECT 1 FROM embedding WHERE key='closed'").fetchone()
        # the trace keeps the page from being downloaded and read again
        from vs.fetch.polite import canonical_url
        url = "https://example.org/calls/closed"
        conn.execute("UPDATE opportunity SET dedup_key=? WHERE title_en='closed'", (canonical_url(url),))
        assert pipeline.prepare_item(None, conn, RawItem(url=url, title="closed", source_id="listing")) == "unchanged"
        arch = housekeeping.archive(conn)
        assert [r["title_en"] for r in arch["rejected"]] == ["closed-applied"]
        assert [r["title_en"] for r in arch["not_applied"]] == ["closed-saved"]


def test_a_call_without_a_deadline_is_over_when_its_source_stops_listing_it():
    future = (date.today() + timedelta(days=40)).isoformat()
    with db.session() as conn:
        _reset(conn)
        _add(conn, "missed-twice", missed=2)
        _add(conn, "missed-once", missed=1)
        _add(conn, "unseen", last_seen=_ago(70))
        _add(conn, "with-deadline", missed=5, deadline=future)  # a feed may drop calls that are still open
        out = housekeeping.clean(conn, PREFS)
        assert out["gone"] == 2
        assert [_status(conn, k) for k in ("missed-twice", "missed-once", "unseen", "with-deadline")] == ["removed", "open", "removed", "open"]


def test_listing_counts_missed_runs_only_for_complete_listings_of_listing_sources():
    from vs.agents import Explorer, Run
    with db.session() as conn:
        _reset(conn)
        _add(conn, "https://a.org/1", source="listing")
        _add(conn, "https://a.org/2", source="listing")
        _add(conn, "https://a.org/s", source="search")
    run = Run(client=None, portfolio=None, model="m", prefs={}, force=True, rerank=False, say=print, jobs=[])
    ex = Explorer(run)
    ex.conn = db.connect()
    try:
        ex._seen_on_source("listing", {"adapter": "html_list"}, None, [RawItem(url="https://a.org/1", title="1", source_id="listing")])
        ex._seen_on_source("search", {"adapter": "jobspy"}, None, [])
        ex.conn.commit()
        missed = dict(ex.conn.execute("SELECT dedup_key, missed_runs FROM opportunity").fetchall())
    finally:
        ex.conn.close()
    assert missed == {"https://a.org/1": 0, "https://a.org/2": 1, "https://a.org/s": 0}


def test_recap_is_offered_once_a_month_and_forgets_unticked_calls():
    from vs.web.app import app
    past = (date.today() - timedelta(days=3)).isoformat()
    with db.session() as conn:
        _reset(conn)
        keep = _add(conn, "keep-me", status="expired", deadline=past)
        drop = _add(conn, "drop-me", status="expired", deadline=past)
        conn.execute("INSERT INTO user_state(opportunity_key, favourite) VALUES('keep-me', 1), ('drop-me', 1)")
    client = TestClient(app)
    assert 'id="recap-offer"' in client.get("/archive").text
    assert 'id="recap-offer"' not in client.get("/archive").text  # once a day at most
    page = client.get("/recap").text
    assert "keep-me" in page and "drop-me" in page
    client.post("/recap/done", data={"kept": [str(keep)], "seen": [str(keep), str(drop)]})
    with db.session() as conn:
        assert not housekeeping.recap_due(conn)
        housekeeping.clean(conn, PREFS)
        assert _status(conn, "keep-me") == "expired" and _status(conn, "drop-me") == "removed"


def test_pause_and_resume_a_source_on_your_copy(monkeypatch):
    from vs.web import app as app_mod
    saved = {}

    def fake_update(mutate):
        doc = {"fetch": {"paused_sources": list(saved.get("p", []))}}
        mutate(doc)
        saved["p"] = doc["fetch"]["paused_sources"]
    monkeypatch.setattr(app_mod, "update_prefs", fake_update)
    monkeypatch.setattr(app_mod, "prefs", lambda: {"sources": {"noisy": {}}})
    client = TestClient(app_mod.app)
    client.post("/sources/pause", data={"sid": "noisy", "on": 1})
    assert saved["p"] == ["noisy"]
    client.post("/sources/pause", data={"sid": "noisy", "on": 0})
    assert saved["p"] == []
    client.post("/sources/pause", data={"sid": "unknown", "on": 1})
    assert saved["p"] == []
