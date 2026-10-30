"""Your hard rules, applied to titles only (never to the AI's scores):

- intake: for sources marked `title_filter = true` (big institutions that post
  every kind of job), a posting counts only if its title names one of your
  fields ([intake].field_titles);
- never-show: titles matching a term in [taste].hide_if, unless the title also
  names one of your fields (a UX role at an accounting firm stays).

Reversible: matching items get status 'hidden' with the reason, and come back
when the rules change. Items you saved (favourite or a status) are never hidden.
"""

from __future__ import annotations

import re

from vs import expiry
from vs.config import prefs
from vs.score import _kw_regex


def compile_rules(p: dict | None = None) -> dict:
    p = p or prefs()
    if not p.get("intake", {}).get("enabled", False):  # off unless you switch it on
        return {"field_rx": None, "title_filter": set(), "hide": []}
    fields = p.get("intake", {}).get("field_titles", "")
    return {
        "field_rx": re.compile(fields, re.I) if fields else None,
        "title_filter": {sid for sid, cfg in p.get("sources", {}).items() if cfg.get("title_filter")},
        "hide": [(term, re.compile(_kw_regex(term), re.I)) for term in p.get("taste", {}).get("hide_if", []) if term.strip()],
    }


def reason(title: str, field: str, source_id: str, rules: dict) -> str:
    """Why this should be hidden, or '' to keep it. Titles only: a field the
    model guessed must never be the reason something disappears."""
    names_your_field = bool(rules["field_rx"] and rules["field_rx"].search(title))
    for term, rx in rules["hide"]:
        # a design role at an accounting firm is still a design role
        if rx.search(title) and not names_your_field:
            return f"never-show: {term}"
    if source_id in rules["title_filter"] and rules["field_rx"] and not rules["field_rx"].search(title):
        return "title doesn't name one of your fields"
    return ""


def apply(conn, *, dry_run: bool = False) -> dict:
    """Hide / restore stored opportunities. Returns counts and the changes."""
    rules = compile_rules()
    saved = {r["opportunity_key"] for r in conn.execute(
        "SELECT opportunity_key FROM user_state WHERE favourite = 1 OR app_status IS NOT NULL")}
    rows = conn.execute(
        "SELECT id, dedup_key, source_id, title_en, title_original, field_tag, status, deadline, hidden_reason, "
        "type, deadline_kind, start_date, other_dates "
        "FROM opportunity WHERE extraction_status = 'done' AND status IN ('open', 'expired', 'hidden')").fetchall()
    hidden, restored = [], []
    for r in rows:
        title = " ".join(filter(None, [r["title_en"], r["title_original"]]))
        why = "" if r["dedup_key"] in saved else reason(title, r["field_tag"] or "", r["source_id"] or "", rules)
        if why and r["status"] != "hidden":
            hidden.append((r["source_id"], r["title_en"] or r["title_original"], why))
            if not dry_run:
                conn.execute("UPDATE opportunity SET status = 'hidden', hidden_reason = ? WHERE id = ?", (why, r["id"]))
        elif not why and r["status"] == "hidden":
            restored.append((r["source_id"], r["title_en"] or r["title_original"], r["hidden_reason"]))
            if not dry_run:
                status = expiry.status_for(dict(r))
                conn.execute("UPDATE opportunity SET status = ?, hidden_reason = NULL WHERE id = ?", (status, r["id"]))
    return {"hidden": hidden, "restored": restored}
