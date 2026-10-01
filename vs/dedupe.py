"""When are two listings the same call?

Two failure modes matter equally: showing one call twice, and hiding a
different opportunity inside another (e.g. FAO's internship programmes for
Europe and for Asia are near-identical texts but different positions). So text
similarity alone never merges; it needs an exact anchor (same organisation and
practically the same role, or the same deadline)."""

from __future__ import annotations

import re
from datetime import date

from rapidfuzz import fuzz

ORG_SAME = 85        # token_set_ratio of organisation names
TITLE_SAME = 92      # token_sort_ratio of roles: word order may differ, extra words may not
COS_SAME = 0.93      # embedding cosine for the cross-site rule (always with a matching deadline)
STOP = {"the", "a", "an", "il", "la", "de", "het", "der", "die", "at", "in", "for", "of", "bij", "presso", "en", "and", "un"}


def _words(s: str | None) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9à-ÿ]+", " ", (s or "").lower()).split() if w not in STOP]


def _role(o: dict) -> str:
    """The title without the organisation's name or the city ('Creative
    Internship at Haines in London' -> 'creative internship')."""
    drop = set(_words(o.get("org_name"))) | set(_words(o.get("location_city")))
    words = [w for w in _words(o.get("title_en")) if w not in drop]
    return " ".join(words) or " ".join(_words(o.get("title_en")))


def _days_apart(a: str, b: str) -> int | None:
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return None


def why_same(a: dict, b: dict, cos: float | None = None) -> str:
    """Reason the two are the same call, or '' if they are not."""
    org = fuzz.token_set_ratio(" ".join(_words(a.get("org_name"))), " ".join(_words(b.get("org_name")))) \
        if a.get("org_name") and b.get("org_name") else 0
    title = fuzz.token_sort_ratio(_role(a), _role(b))
    dl_a, dl_b = (a.get("deadline") or "")[:10], (b.get("deadline") or "")[:10]
    gap = _days_apart(dl_a, dl_b) if dl_a and dl_b else None
    same_deadline = gap is not None and gap <= 1          # one day of slack for time zones
    compatible_deadline = same_deadline or not dl_a or not dl_b
    city_a, city_b = " ".join(_words(a.get("location_city"))), " ".join(_words(b.get("location_city")))
    same_place = not city_a or not city_b or fuzz.ratio(city_a, city_b) >= 85

    # 1. Same organisation, practically the same role, compatible deadline and place.
    if org >= ORG_SAME and title >= TITLE_SAME and compatible_deadline and same_place:
        return f"same organisation and role (org {org:.0f}, role {title:.0f})"
    # 2. The same call announced on two sites: matching deadline, same place,
    #    near-identical content and a resemblance in organisation or role.
    if cos is not None and same_deadline and same_place and cos >= COS_SAME and (org >= 60 or title >= 70):
        return f"same deadline and content (cos {cos:.2f}, org {org:.0f}, role {title:.0f})"
    return ""
