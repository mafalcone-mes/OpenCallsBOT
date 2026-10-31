"""The to-do board: your saved calls and applications, what the briefs ask of
your portfolio, and your own items, in three bands (To do, In progress,
Done) plus a Deleted view to restore from. It replaces the Applications page.

- A call's band is its application status (saved or shortlisted = To do,
  preparing = In progress, submitted and the outcomes = Done); moving the card
  changes the status, so the feed, calendar and pop-up agree.
- A call still to send lists the documents it asks for (its `required_docs`,
  or ones you add when the page didn't say), each ticked off by you; the
  portfolio ticks itself once its brief has a portfolio PDF (you can untick
  it). What is missing sorts the call into a group: letter, portfolio, both,
  other documents, ready to send, requirements unknown. Kept in kv `todo_docs`.
- Portfolio items come from what the briefs say to stress in your projects;
  their band is kept in kv `todo_state`, their notes in kv `todo_notes`.
- Your own items live in the `todo` table (text, kind, date, linked call, note)."""

from __future__ import annotations

from datetime import date, timedelta

from vs import db, present

ACTIVE = ("shortlisted", "preparing")  # applications still to send
STATES = [("todo", "To do"), ("progress", "In progress"), ("done", "Done")]
KINDS = [("call", "Call"), ("portfolio", "Portfolio")]
OUTCOMES = [("selected", "Selected"), ("rejected", "Rejected"), ("no_answer", "No answer")]
GROUPS = [("both", "Missing letter and portfolio"), ("letter", "Missing motivation letter"), ("portfolio", "Missing portfolio"),
          ("other", "Missing other documents"), ("ready", "Ready to send"), ("unknown", "Requirements unknown")]
ALERT_DAYS = 7
_COLUMN = {None: "todo", "": "todo", "shortlisted": "todo", "preparing": "progress", "submitted": "done",
           "selected": "done", "rejected": "done", "no_answer": "done"}
_STATUS = {"todo": "shortlisted", "progress": "preparing", "done": "submitted"}
PORTFOLIO_DOCS = ("portfolio", "work_samples")
# The next step names the first missing document, in this order.
NEXT = {"portfolio": "prepare the portfolio", "work_samples": "choose the work samples", "motivation_letter": "write the motivation letter",
        "project_proposal": "write the project proposal", "budget": "draw up the budget", "cv": "update your CV",
        "writing_sample": "choose a writing sample", "video": "prepare the video", "references": "ask for references",
        "diploma": "find your diploma", "id_document": "scan your ID", "other": "prepare the other documents"}


def _calls(conn) -> list[dict]:
    """Every call you saved or gave a status, not dismissed."""
    return [db.row_dict(r) for r in conn.execute(
        """SELECT o.id, o.title_en, o.title_original, o.org_name, o.deadline, o.deadline_kind, o.canonical_url,
                  o.status, o.dedup_key, o.required_docs, u.app_status, u.favourite, u.notes, u.updated, m.final
           FROM opportunity o JOIN user_state u ON u.opportunity_key = o.dedup_key
           LEFT JOIN match m ON m.opportunity_id = o.id
           WHERE COALESCE(u.dismissed, 0) = 0 AND (u.favourite = 1 OR u.app_status IS NOT NULL)
           ORDER BY COALESCE(NULLIF(o.deadline, ''), '9999'), o.id""")]


def closing_soon(conn, days: int = ALERT_DAYS) -> list[dict]:
    """Saved calls and applications to send that close within `days`."""
    today, last = date.today().isoformat(), (date.today() + timedelta(days=days)).isoformat()
    return [o for o in _calls(conn) if o["status"] == "open" and _COLUMN.get(o["app_status"]) != "done"
            and o["deadline"] and today <= o["deadline"][:10] <= last]


def _state(conn) -> dict:
    return db.kv_get(conn, "todo_state") or {}


def docs_for(o: dict, brief: dict | None, rec: dict) -> list[dict]:
    """The documents a call asks for (and ones you added), each with its
    limits and whether it is done. `rec`: your ticks for this call."""
    asked = [d for d in o.get("required_docs") or [] if isinstance(d, dict)]
    asked += [{"kind": k, "details": "added by you", "required": True} for k in rec.get("added", [])]
    has_pdf = bool(((brief or {}).get("portfolio_pdf") or {}).get("path"))
    out, seen = [], {}
    for d in asked:
        kind = d.get("kind") if d.get("kind") in present.DOC_LABELS else "other"
        seen[kind] = seen.get(kind, 0) + 1
        doc_id = kind if seen[kind] == 1 else f"{kind}-{seen[kind]}"
        ticked = rec.get("done", {}).get(doc_id)
        auto = ticked is None and kind in PORTFOLIO_DOCS and has_pdf
        details = (d.get("details") or "").strip()
        if details.lower().strip(" .") in ("unknown", "not specified", "n/a", "none", "-"):  # the reader's "don't know"
            details = ""
        out.append({"id": doc_id, "kind": kind, "label": present.DOC_LABELS[kind], "details": details,
                    "required": d.get("required", True) is not False, "done": bool(ticked) or auto, "auto": auto})
    return out


def _group(docs: list[dict]) -> str:
    if not docs:
        return "unknown"
    missing = {d["kind"] for d in docs if d["required"] and not d["done"]}
    letter, port = "motivation_letter" in missing, bool(missing & set(PORTFOLIO_DOCS))
    return "both" if letter and port else "letter" if letter else "portfolio" if port else "other" if missing else "ready"


def _next_step(docs: list[dict], brief) -> str:
    if not docs:
        return "Next: check what they ask for"
    missing = [d for d in docs if d["required"] and not d["done"]]
    if not missing:
        return "Ready: send the application"
    first = min(missing, key=lambda d: list(NEXT).index(d["kind"]))
    step = "prepare the brief and portfolio PDF" if first["kind"] in PORTFOLIO_DOCS and not brief else NEXT[first["kind"]]
    return f"Next: {step}" + (f" (and {len(missing) - 1} more)" if len(missing) > 1 else "")


def board(conn, briefing: set[int] | frozenset = frozenset()) -> dict:
    """{"todo": [card], "progress": [...], "done": [...], "deleted": [...]}.
    `briefing`: calls whose brief is being prepared right now."""
    state, all_docs = _state(conn), db.kv_get(conn, "todo_docs") or {}
    notes = db.kv_get(conn, "todo_notes") or {}
    out: dict[str, list[dict]] = {"todo": [], "progress": [], "done": [], "deleted": []}
    calls = _calls(conn)
    names = {o["id"]: o["title_en"] or o["title_original"] or "Untitled call" for o in calls}

    def card(**kw):
        base = {"org": "", "sub": "", "due": "", "href": "", "link": "", "outcome": None, "oid": None, "note": "",
                "docs": [], "group": None, "score": None, "changed": "", "own": None, "briefing": False, "has_brief": False}
        return {**base, **kw}

    today, month_ago = date.today().isoformat(), (date.today() - timedelta(days=30)).isoformat()
    for o in calls:
        oid, name = o["id"], names[o["id"]]
        col = _COLUMN.get(o["app_status"], "todo")
        # The board is what's still to do: a call that closed before you sent it,
        # and outcomes older than a month, are in the Archive.
        closed = o["status"] != "open" or bool(o["deadline"] and o["deadline"][:10] < today)
        if (col != "done" and closed) or (col == "done" and (o["updated"] or "")[:10] < month_ago):
            continue
        brief = db.kv_get(conn, f"brief:{oid}")
        docs, group = [], None
        if col == "done":
            outcome = dict(OUTCOMES).get(o["app_status"])
            sub = outcome or "Submitted · waiting for an answer"
        else:
            outcome = None
            docs = docs_for(o, brief, all_docs.get(str(oid), {}))
            group, sub = _group(docs), _next_step(docs, brief)
        if oid in briefing:
            sub = "Preparing the brief and portfolio PDF…"
        out[col].append(card(key=f"call:{oid}", kind="call", title=name, org=o["org_name"] or "", sub=sub,
                             due=(o["deadline"] or "") if col != "done" else "", href=f"/o/{oid}", link=o["canonical_url"] or "",
                             state=col, outcome=o["app_status"] if outcome else None, oid=oid, note=o["notes"] or "",
                             docs=docs, group=group, score=o["final"], changed=o["updated"] or "", briefing=oid in briefing,
                             has_brief=bool(brief)))
        if col == "done" or not brief:
            continue
        for s in brief.get("selected") or []:
            if not s.get("stress"):
                continue
            proj = s.get("project") or s.get("angle", "").split(":")[0]
            key = f"stress:{oid}:{proj}"
            st = state.get(key, "todo")
            out[st if st in out else "todo"].append(card(
                key=key, kind="portfolio", title=f"{proj}: {s['stress']}", sub=f"For {name}", due=o["deadline"] or "",
                href=f"/o/{oid}#brief", state=st, oid=oid, note=notes.get(key, ""), score=o["final"], changed=o["updated"] or ""))

    for r in conn.execute("SELECT * FROM todo ORDER BY created"):
        st = r["state"] or "todo"
        call = r["oid"] if r["oid"] in names else None
        out[st if st in out else "todo"].append(card(
            key=f"own:{r['id']}", kind=r["section"] if r["section"] in dict(KINDS) else "portfolio", title=r["text"],
            sub=f"For {names[call]}" if call else "", due=r["due"] or "", href=f"/o/{call}" if call else "", state=st,
            oid=call, note=r["note"] or "", changed=r["created"] or "",
            own={"id": r["id"], "text": r["text"], "kind": r["section"], "due": r["due"] or "", "oid": call}))

    # deleted calls are no longer saved: they are listed from the kv record
    saved_again = {f"call:{o['id']}" for o in calls}  # saved again from the feed: back on the board
    for key, v in state.items():
        if key.startswith("call:") and isinstance(v, dict) and v.get("state") == "deleted" and key not in saved_again:
            row = conn.execute("SELECT id, title_en, title_original, org_name FROM opportunity WHERE id=?", (int(key[5:]),)).fetchone()
            if row:
                out["deleted"].append(card(key=key, kind="call", title=row["title_en"] or row["title_original"], org=row["org_name"] or "",
                                           sub="Removed from your saved calls", href=f"/o/{row['id']}", state="deleted", oid=row["id"]))
    for col in ("todo", "progress"):  # soonest deadline first
        out[col].sort(key=lambda c: (c["due"] or "")[:10] or "9999")
    return out


def group_counts(b: dict) -> dict[str, int]:
    """How many calls still to send are in each group (for the filter chips)."""
    counts = {g: 0 for g, _ in GROUPS}
    for c in b["todo"] + b["progress"]:
        if c["group"]:
            counts[c["group"]] += 1
    return counts


def find(b: dict, key: str) -> dict | None:
    return next((c for col in b.values() for c in col if c["key"] == key), None)


def open_count(conn) -> int:
    b = board(conn)
    return len(b["todo"]) + len(b["progress"])


def call_options(conn) -> list[tuple[int, str]]:
    """Calls an own item can be linked to."""
    return [(o["id"], o["title_en"] or o["title_original"] or "Untitled call") for o in _calls(conn)
            if _COLUMN.get(o["app_status"]) != "done"]


def act(conn, keys: list[str], action: str) -> list[tuple[str, str]]:
    """Apply one action to several cards. Returns the status signals to log
    for calls, [(opportunity id, kind:value)], which the caller sends through
    log_signal (they also teach the feed)."""
    state = _state(conn)
    signals: list[tuple[str, str]] = []
    for key in keys:
        kind, _, rest = key.partition(":")
        if kind == "own" and rest.isdigit():
            if action in ("todo", "progress", "done", "deleted"):
                conn.execute("UPDATE todo SET state=? WHERE id=?", (action, int(rest)))
            elif action == "restore":
                conn.execute("UPDATE todo SET state='todo' WHERE id=?", (int(rest),))
        elif kind == "call" and rest.isdigit():
            row = conn.execute("SELECT u.app_status FROM opportunity o JOIN user_state u ON u.opportunity_key=o.dedup_key "
                               "WHERE o.id=?", (int(rest),)).fetchone()
            prev = row["app_status"] if row else None
            if action in _STATUS:
                if not (action == "done" and _COLUMN.get(prev) == "done"):  # moving within Done keeps the outcome
                    signals.append((rest, "status:" + _STATUS[action]))
            elif action in dict(OUTCOMES):
                signals.append((rest, "status:" + action))
            elif action == "deleted":
                state[key] = {"state": "deleted", "prev": prev, "at": db.now()}
                signals += [(rest, "status:"), (rest, "unfavourite")]
            elif action == "restore":
                rec = state.pop(key, None) or {}
                signals.append((rest, "favourite"))
                if rec.get("prev"):
                    signals.append((rest, "status:" + rec["prev"]))
        elif kind == "stress":
            if action in ("todo", "progress", "done", "deleted"):
                state[key] = action
            elif action == "restore":
                state.pop(key, None)
    db.kv_set(conn, "todo_state", state)
    return signals


def set_doc(conn, oid: int, doc_id: str, on: bool) -> None:
    docs = db.kv_get(conn, "todo_docs") or {}
    docs.setdefault(str(oid), {}).setdefault("done", {})[doc_id] = on
    db.kv_set(conn, "todo_docs", docs)


def add_doc(conn, oid: int, kind: str) -> None:
    """A document the call asks for that its page didn't list (or the app missed)."""
    if kind not in present.DOC_LABELS:
        return
    docs = db.kv_get(conn, "todo_docs") or {}
    docs.setdefault(str(oid), {}).setdefault("added", []).append(kind)
    db.kv_set(conn, "todo_docs", docs)


def set_note(conn, key: str, text: str) -> int | None:
    """Saves a card's note. For a call returns its id: its note is the one on
    the call's page, which the caller saves through log_signal."""
    text = text.strip()[:1000]
    kind, _, rest = key.partition(":")
    if kind == "call" and rest.isdigit():
        return int(rest)
    if kind == "own" and rest.isdigit():
        conn.execute("UPDATE todo SET note=? WHERE id=?", (text, int(rest)))
    elif kind == "stress":
        notes = db.kv_get(conn, "todo_notes") or {}
        notes[key] = text
        db.kv_set(conn, "todo_notes", notes)
    return None


def _own_fields(text: str, kind: str, due: str, oid: str) -> tuple[str, str, str, int | None]:
    text = " ".join(text.split())[:300]
    kind = kind if kind in dict(KINDS) else "portfolio"
    try:
        due = date.fromisoformat(due).isoformat() if due else ""
    except ValueError:
        due = ""
    return text, kind, due, int(oid) if str(oid).isdigit() else None


def add_own(conn, text: str, kind: str, due: str = "", oid: str = "") -> None:
    text, kind, due, call = _own_fields(text, kind, due, oid)
    if text:
        conn.execute("INSERT INTO todo(section, text, created, state, due, oid) VALUES(?,?,?, 'todo', ?, ?)",
                     (kind, text, db.now(), due, call))


def edit_own(conn, tid: int, text: str, kind: str, due: str = "", oid: str = "") -> None:
    text, kind, due, call = _own_fields(text, kind, due, oid)
    if text:
        conn.execute("UPDATE todo SET text=?, section=?, due=?, oid=? WHERE id=?", (text, kind, due, call, tid))
