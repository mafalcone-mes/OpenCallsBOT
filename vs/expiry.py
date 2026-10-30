"""When an opportunity is over.

A deadline decides when there is one. Without a deadline, the start date
(and, for workshops and schools, the session dates) does: a workshop held
last spring is over even though its page never named a deadline.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta

# Days an opportunity stays open after its last known date, when it has no
# deadline. Events are over once they've happened; a job "starting in
# October" may still be hiring for a while; rolling calls run on and on.
EVENT_TYPES = {"workshop", "summer_school"}
GRACE_DAYS = {"event": 0, "default": 30, "rolling": 365}


def _end_of(value: str) -> date | None:
    """'2026-10-03' -> that day; '2026-10' -> 31 Oct; '2026' -> 31 Dec."""
    value = (value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value):
            return date.fromisoformat(value[:10])
        if re.fullmatch(r"\d{4}-\d{2}", value):
            y, m = map(int, value.split("-"))
            return date(y, m, calendar.monthrange(y, m)[1])
        if re.fullmatch(r"\d{4}", value):
            return date(int(value), 12, 31)
    except ValueError:
        pass
    return None


def _other_dates(o: dict) -> list[str]:
    raw = o.get("other_dates") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [d.get("date") or "" for d in raw if isinstance(d, dict)]


def ends_on(o: dict) -> date | None:
    """The day after which the opportunity is over, or None if we can't tell."""
    if o.get("deadline"):
        return _end_of(o["deadline"])
    # Sessions date an event; on any other page "other dates" are info
    # sessions or history ("since 2006"), not when the opportunity ends.
    event = o.get("type") in EVENT_TYPES
    dates = [o.get("start_date") or "", *(_other_dates(o) if event else [])]
    known = [d for d in map(_end_of, dates) if d]
    if not known:
        return None
    if o.get("deadline_kind") == "rolling":
        grace = GRACE_DAYS["rolling"]
    elif event:
        grace = GRACE_DAYS["event"]
    else:
        grace = GRACE_DAYS["default"]
    return max(known) + timedelta(days=grace)


def status_for(o: dict, today: date | None = None) -> str:
    end = ends_on(o)
    return "expired" if end and end < (today or date.today()) else "open"


def expire(conn) -> int:
    """Mark open opportunities that are over as expired. Returns how many."""
    rows = conn.execute(
        "SELECT id, type, deadline, deadline_kind, start_date, other_dates FROM opportunity WHERE status = 'open'").fetchall()
    over = [r["id"] for r in rows if status_for(dict(r)) == "expired"]
    conn.executemany("UPDATE opportunity SET status = 'expired' WHERE id = ?", [(i,) for i in over])
    return len(over)
