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
