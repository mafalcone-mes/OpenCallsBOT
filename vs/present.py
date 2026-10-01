"""Human-readable formatting shared by the dashboard, reports and briefs."""

from __future__ import annotations

from datetime import date

TYPE_LABELS = {
    "job": "Job", "residency": "Residency", "open_call": "Open call", "grant": "Grant", "commission": "Commission",
    "competition": "Competition", "workshop": "Workshop", "summer_school": "Summer school", "fellowship": "Fellowship",
    "phd": "PhD", "research_position": "Research position", "other": "Other",
}
MONEY_LABELS = {
    "salary": "Paid", "stipend": "Stipend", "grant": "Grant", "fee_paid_to_you": "Fee paid to you", "prize": "Prize",
    "covers_costs": "Covers costs", "free_no_support": "Free, no support", "fee_required": "Fee required",
    "unknown": "Money unclear",
}
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


def deadline_label(deadline: str, kind: str = "") -> str:
    d = days_left(deadline)
    if d is None:
        return "Rolling" if kind == "rolling" else "No deadline found"
    when = date.fromisoformat(deadline[:10]).strftime("%d %b %Y")
    if d < 0:
        return f"Closed {when}"
    if d == 0:
        return f"Today · {when}"
    return f"{d} days · {when}"


def location(o: dict) -> str:
    bits = [o.get("location_city") or "", o.get("location_country") or ""]
    loc = ", ".join(b for b in bits if b)
    mode = o.get("remote_mode") or ""
    if mode in ("remote", "online"):
        return f"{mode.title()}{' · ' + loc if loc else ''}"
    if mode == "hybrid":
        return f"Hybrid · {loc}" if loc else "Hybrid"
    return loc or "Location unclear"
