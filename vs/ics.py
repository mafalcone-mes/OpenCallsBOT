"""Deadlines as .ics calendar files (per opportunity, or everything tracked)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from icalendar import Alarm, Calendar, Event


def _calendar() -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//vacancies-searcher//EN")
    cal.add("version", "2.0")
    return cal


def _event(uid: str, title: str, day: str, url: str, description: str, alarms: bool) -> Event | None:
    try:
        d = date.fromisoformat(day[:10])
    except (TypeError, ValueError):
        return None
    ev = Event()
    ev.add("uid", f"{uid}@vacancies-searcher")
    ev.add("summary", title)
    ev.add("dtstart", d)
    ev.add("dtend", d + timedelta(days=1))
    ev.add("dtstamp", datetime.now(timezone.utc))
    if url:
        ev.add("url", url)
    ev.add("description", f"{description}\n\n{url}".strip())
    if alarms:
        for days in (7, 2):
            al = Alarm()
            al.add("action", "DISPLAY")
            al.add("description", f"{title}: {days} days left")
            al.add("trigger", timedelta(days=-days))
            ev.add_component(al)
    return ev


def opportunity_events(o: dict) -> list[Event]:
    url = o.get("canonical_url") or ""
    name = o.get("title_en") or o.get("title_original") or "Opportunity"
    events = []
    ev = _event(f"opp-{o['id']}-deadline", f"Deadline: {name}", o.get("deadline") or "", url, o.get("summary_en") or "", True)
    if ev:
        events.append(ev)
    for i, other in enumerate(o.get("other_dates") or []):
        ev = _event(f"opp-{o['id']}-d{i}", f"{other.get('label', 'Date')}: {name}", other.get("date") or "", url, "", False)
        if ev:
            events.append(ev)
    return events


def to_ics(opportunities: list[dict]) -> bytes:
    cal = _calendar()
    for o in opportunities:
        for ev in opportunity_events(o):
            cal.add_component(ev)
    return cal.to_ical()
