"""Two listings of the same call merge into one: the one you saved (applied
to, wrote on) stays visible, with your state, and a past merge that hid it is
undone (the EMAP case: saved on one site, then found on another)."""

import json

import numpy as np

from vs import db, dedupe, embed, examples, pipeline

A = "https://transartists.org/emap"   # found first
B = "https://noemalab.eu/emap"        # found later: the one you saved


def _reset(conn):
    for t in ("opportunity", "user_state", "embedding"):
        conn.execute(f"DELETE FROM {t}")


def _add(conn, key, first_seen, status="open", urls=None):
    conn.execute(
        "INSERT INTO opportunity(dedup_key, canonical_url, source_urls, source_id, title_en, org_name, deadline, "
        "extraction_status, status, first_seen, last_seen) VALUES(?,?,?,?,?,?,?, 'done', ?, ?, ?)",
        (key, key, json.dumps(urls or [key]), "test", "EMAP residencies", "EMAP", "2099-11-06", status, first_seen, first_seen))


def _same(monkeypatch):
    monkeypatch.setattr(dedupe, "why_same", lambda a, b, cos=None: "same call")
    monkeypatch.setattr(embed, "ensure", lambda conn, kind, items, model: {k: np.ones(2, dtype=np.float32) for k in items})


def _row(conn, key):
    return db.row_dict(conn.execute("SELECT * FROM opportunity WHERE dedup_key=?", (key,)).fetchone())


def test_the_saved_listing_is_the_one_kept(monkeypatch):
    _same(monkeypatch)
    with db.session() as conn:
        _reset(conn)
        _add(conn, A, "2026-10-29T09:00:00+00:00")
        _add(conn, B, "2026-11-02T12:00:00+00:00")
    examples.log_signal("favourite", key=B)
    with db.session() as conn:
        assert pipeline.merge_near_duplicates(conn, "m") == 1
        assert _row(conn, B)["status"] == "open" and _row(conn, A)["status"] == "duplicate"
        assert set(_row(conn, B)["source_urls"]) == {A, B}


def test_a_past_merge_that_hid_your_save_is_undone(monkeypatch):
    _same(monkeypatch)
    with db.session() as conn:
        _reset(conn)
        _add(conn, A, "2026-10-29T09:00:00+00:00", urls=[A, B])
        _add(conn, B, "2026-11-02T12:00:00+00:00", status="duplicate")
    examples.log_signal("favourite", key=B)
    with db.session() as conn:
        pipeline.merge_near_duplicates(conn, "m")
        assert _row(conn, B)["status"] == "open" and _row(conn, A)["status"] == "duplicate"
        # and it stays that way at the next refresh
        assert pipeline.merge_near_duplicates(conn, "m") == 0
        assert _row(conn, B)["status"] == "open"


def test_when_both_hold_something_the_kept_one_gets_it_all_and_a_rebuild_keeps_it(monkeypatch):
    _same(monkeypatch)
    with db.session() as conn:
        _reset(conn)
        _add(conn, A, "2026-10-29T09:00:00+00:00")
        _add(conn, B, "2026-11-02T12:00:00+00:00")
    examples.log_signal("status", key=A, status="preparing")   # A weighs more: it stays
    examples.log_signal("favourite", key=B)
    examples.log_signal("note", key=B, text="ask about the fee")
    with db.session() as conn:
        pipeline.merge_near_duplicates(conn, "m")
        assert _row(conn, A)["status"] == "open" and _row(conn, B)["status"] == "duplicate"
    examples.rebuild_user_state()
    with db.session() as conn:
        s = conn.execute("SELECT favourite, app_status, notes FROM user_state WHERE opportunity_key=?", (A,)).fetchone()
    assert (s["favourite"], s["app_status"], s["notes"]) == (1, "preparing", "ask about the fee")


def test_without_any_state_the_older_listing_stays(monkeypatch):
    _same(monkeypatch)
    with db.session() as conn:
        _reset(conn)
        _add(conn, A, "2026-10-29T09:00:00+00:00")
        _add(conn, B, "2026-11-02T12:00:00+00:00")
        pipeline.merge_near_duplicates(conn, "m")
        assert _row(conn, A)["status"] == "open" and _row(conn, B)["status"] == "duplicate"
