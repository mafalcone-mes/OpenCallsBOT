"""Human-readable formatting shared by the dashboard, reports and briefs."""

from __future__ import annotations

from datetime import date, datetime

TYPE_LABELS = {
    "job": "Job", "residency": "Residency", "open_call": "Open call", "grant": "Grant", "commission": "Commission",
    "competition": "Competition", "workshop": "Workshop", "summer_school": "Summer school", "fellowship": "Fellowship",
    "phd": "PhD", "research_position": "Research position", "other": "Other",
}
# Feed tabs, in display order. Any type not listed (or missing) goes to "other",
# so a new type the extractor starts using still shows up.
FEED_GROUPS = [
    ("calls", "Open calls", ("open_call", "competition", "commission")),
    ("residencies", "Residencies", ("residency",)),
    ("funding", "Grants and fellowships", ("grant", "fellowship")),
    ("learning", "Workshops and schools", ("workshop", "summer_school")),
    ("research", "Research and PhDs", ("phd", "research_position")),
    ("jobs", "Jobs", ("job",)),
    ("other", "Other", ()),
]
FEED_PAGE = 200


def far_below() -> float:
    """Scores below this (0-1) go to the bottom of the feed, under "Far from your work"."""
    from vs.config import prefs
    return float(prefs().get("weights", {}).get("far_below", 40)) / 100


def feed_group_of(type_: str | None) -> str:
    for gid, _, types in FEED_GROUPS:
        if type_ in types:
            return gid
    return "other"


MONEY_LABELS = {
    "salary": "Paid", "stipend": "Stipend", "grant": "Grant", "fee_paid_to_you": "Fee paid to you", "prize": "Prize",
    "covers_costs": "Covers costs", "free_no_support": "Free, no support", "fee_required": "Fee required",
    "unknown": "Money unclear",
}
# Why you dismiss something (the feed learns from it) and where an application stands.
DISMISS_REASONS = [("not_my_field", "Not my field"), ("not_my_taste", "Not my taste"), ("too_corporate", "Too corporate"),
                   ("wrong_seniority", "Wrong seniority"), ("timing", "Bad timing"), ("location", "Location"),
                   ("money", "Money"), ("ineligible", "Not eligible"), ("duplicate", "Duplicate")]
STATUSES = ["shortlisted", "preparing", "submitted", "selected", "rejected", "no_answer"]
DOC_LABELS = {
    "cv": "CV", "portfolio": "Portfolio", "motivation_letter": "Motivation letter", "project_proposal": "Project proposal",
    "budget": "Budget", "references": "References", "work_samples": "Work samples", "video": "Video",
    "id_document": "ID document", "diploma": "Diploma", "writing_sample": "Writing sample", "other": "Other",
}


def money(amount, currency: str = "") -> str:
    if amount in (None, ""):
        return ""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    sym = {"EUR": "€", "GBP": "£", "USD": "$", "CHF": "CHF "}.get((currency or "").upper(), f"{currency} " if currency else "")
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return f"{sym}{text}"


def money_badge(cond: dict) -> str:
    """e.g. 'Stipend €1,200/month · covers housing, travel · fee €35'."""
    if not cond:
        return "Money unclear"
    kinds = [k for k in cond.get("money_kind") or [] if k != "unknown"]
    parts = []
    main = next((k for k in kinds if k != "fee_required"), None)
    if main:
        label = MONEY_LABELS.get(main, main)
        amt = money(cond.get("amount"), cond.get("currency", ""))
        period = cond.get("period") or ""
        parts.append(f"{label} {amt}{'/' + period if amt and period else ''}".strip())
    covers = cond.get("covers") or []
    if covers:
        parts.append("covers " + ", ".join(c.replace("_", " ") for c in covers))
    if "fee_required" in kinds or cond.get("application_fee"):
        fee = money(cond.get("application_fee"), cond.get("currency", ""))
        parts.append(f"fee {fee}" if fee else "fee required")
    if cond.get("for_organisations"):
        parts.append("for organisations")
    return " · ".join(parts) or "Money unclear"


def days_left(deadline: str) -> int | None:
    if not deadline:
        return None
    try:
        return (date.fromisoformat(deadline[:10]) - date.today()).days
    except ValueError:
        return None


def deadline_parts(deadline: str, kind: str = "") -> tuple[str, str]:
    """('12 days left', '14 Nov 2026'): the first part is read first on a card."""
    d = days_left(deadline)
    if d is None:
        return ("Rolling" if kind == "rolling" else "No deadline found"), ""
    when = date.fromisoformat(deadline[:10]).strftime("%d %b %Y")
    if d < 0:
        return "Closed", when
    if d == 0:
        return "Today", when
    return f"{d} day{'s' if d != 1 else ''} left", when


def deadline_label(deadline: str, kind: str = "") -> str:
    main, when = deadline_parts(deadline, kind)
    if not when:
        return main
    return f"Closed {when}" if main == "Closed" else f"{main.removesuffix(' left')} · {when}"


def save_button(oid: int, on: bool) -> str:
    """The Save toggle, labelled (a star alone reads as favourite, rating or
    featured). Same markup on cards, the detail page and after a click."""
    return (f'<button class="btn save {"on" if on else ""}" hx-post="/o/{oid}/fav" hx-vals=\'{{"on": {0 if on else 1}}}\' '
            f'hx-swap="outerHTML" hx-disabled-elt="this" aria-pressed="{"true" if on else "false"}" title="{"Remove from saved" if on else "Save for later"}">'
            f'<span aria-hidden="true">{"★" if on else "☆"}</span> {"Saved" if on else "Save"}</button>')


def project_chips(suggested: list[dict] | None, n: int = 3) -> list[dict]:
    """Suggested projects for a card: one chip per project, even when the
    model suggested the same project under two categories."""
    out, seen = [], set()
    for s in suggested or []:
        slug, _, cat = (s.get("angle") or "").partition(":")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append({"label": slug.replace("-", " ").title(), "cat": cat, "why": s.get("why") or ""})
        if len(out) == n:
            break
    return out


def run_label(started: str) -> str:
    """When a refresh ran, in local time: '30 Oct, 12:03'."""
    try:
        return datetime.fromisoformat(started).astimezone().strftime("%d %b, %H:%M")
    except ValueError:
        return ""


def location(o: dict) -> str:
    bits = [o.get("location_city") or "", o.get("location_country") or ""]
    loc = ", ".join(b for b in bits if b)
    mode = o.get("remote_mode") or ""
    if mode in ("remote", "online"):
        return f"{mode.title()}{' · ' + loc if loc else ''}"
    if mode == "hybrid":
        return f"Hybrid · {loc}" if loc else "Hybrid"
    return loc or "Location unclear"
