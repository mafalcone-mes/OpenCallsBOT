"""Studios to approach: small studios rarely publish vacancies, so this is a
hand-curated list for spontaneous / internship applications (studios.toml),
plus your contact status per studio (kept in data/signals.jsonl)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from vs.config import APP_DIR, prefs
from vs.examples import log_signal, read_signals

STUDIOS_PATH = APP_DIR / "studios.toml"
STATUSES = ["to approach", "contacted", "replied", "not now"]


def load() -> list[dict]:
    p = prefs()
    if p.get("_from_sheet"):  # the Google Sheet is the master list
        studios = [dict(s) for s in p.get("_studios", [])]
    elif Path(STUDIOS_PATH).exists():
        with open(STUDIOS_PATH, "rb") as f:
            studios = tomllib.load(f).get("studio", [])
    else:
        return []
    watched = {w["url"] for w in p.get("watchlist", [])}
    status = {}
    for e in read_signals():
        if e.get("kind") == "studio_status":
            status[e.get("studio")] = e.get("status")
    for s in studios:
        s["watched"] = bool(s.get("jobs_url") and s["jobs_url"] in watched)
        s["status"] = status.get(s["name"], "to approach")
    return studios


def set_status(name: str, status: str) -> None:
    if status in STATUSES:
        log_signal("studio_status", studio=name, status=status)
