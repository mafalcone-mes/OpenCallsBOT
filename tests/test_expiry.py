"""An opportunity is over when its deadline passed, or, with no deadline,
when its last known date did (cases taken from real La Scuola Open Source,
IJ4EU and job pages)."""

from datetime import date

from vs import db, expiry

TODAY = date(2026, 10, 6)


def over(**o) -> bool:
    return expiry.status_for(o, TODAY) == "expired"


def test_deadline_decides_when_there_is_one():
    assert over(deadline="2026-10-05", start_date="2027-01-01")
    assert not over(deadline="2026-10-06", start_date="2025-01-01", type="workshop")


def test_workshop_without_deadline_is_over_after_its_last_session():
    # Algorave: first session 4 Nov 2025, second 11 Nov 2025
    assert over(type="workshop", start_date="2025-11-04", other_dates='[{"label": "2nd session", "date": "2025-11-11"}]')
    assert not over(type="workshop", start_date="2026-10-01", other_dates=[{"label": "last day", "date": "2026-10-08"}])


def test_jobs_starting_this_month_stay_open_for_a_while():
    assert not over(type="job", start_date="2026-10-01")
    assert over(type="job", start_date="2026-08-01")


def test_rolling_calls_get_a_year_and_partial_dates_count_to_the_end():
    assert over(type="grant", deadline_kind="rolling", start_date="2024-01-01")   # IJ4EU 2024 scheme
    assert not over(type="residency", start_date="2026-10")                       # "October 2026"
    assert not over(type="residency", start_date="2026")


def test_other_dates_only_count_for_events():
    # an info session or "since 2006" on a jobs page doesn't close the call
    assert not over(type="job", other_dates=[{"label": "info session", "date": "2026-08-26"}])
    assert not over(type="job", deadline_kind="rolling", other_dates=[{"label": "since", "date": "2006-01-01"}])


def test_no_dates_at_all_means_open():
    assert not over(type="workshop")
    assert not over(type="job", start_date="soon", other_dates="not json")


def test_expire_updates_only_open_rows_that_are_over():
    with db.session() as conn:
        conn.execute("DELETE FROM opportunity")
        for key, type_, start in [("t:old-workshop", "workshop", "2025-02-21"), ("t:future-workshop", "workshop", "2099-01-01")]:
            conn.execute("INSERT INTO opportunity(dedup_key, canonical_url, title_en, type, deadline, start_date, status, "
                         "extraction_status, first_seen) VALUES(?,?,?,?,'',?,'open','done',?)", (key, key, key, type_, start, db.now()))
        assert expiry.expire(conn) == 1
        status = dict(conn.execute("SELECT dedup_key, status FROM opportunity").fetchall())
    assert status == {"t:old-workshop": "expired", "t:future-workshop": "open"}
