"""Local dashboard: FastAPI + Jinja2 + HTMX. Run with `uv run vs serve`."""

from __future__ import annotations

import calendar as cal_mod
import json
import os
import sqlite3
import threading
from datetime import date, timedelta
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from vs import chatlog, db, expiry, llm, pipeline, present, references, todo
from vs import portfolio as portfolio_mod
from vs import config
from vs.config import prefs, update_prefs
from vs.examples import list_examples, log_signal, remove_example, taste_signals
from vs.ics import to_ics
from vs import report
from vs.report import feed_counts, feed_rows

HERE = Path(__file__).parent
app = FastAPI(title="vacancies searcher")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(present=present)


def static_url(name: str) -> str:
    """/static/<name>?v=<last change>, so browsers drop their saved copy after an update."""
    try:
        return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"
    except OSError:
        return f"/static/{name}"


templates.env.globals.update(static_url=static_url)
from vs.web.icons import icon  # noqa: E402
templates.env.globals.update(icon=icon)

STATUSES = present.STATUSES
DISMISS_REASONS = present.DISMISS_REASONS
_brief_jobs: dict[int, dict] = {}


def _portfolio():
    try:
        return portfolio_mod.load()
    except Exception as e:  # show the error instead of crashing every page
        return e


def ctx(request: Request, **kw) -> dict:
    p = _portfolio()
    with db.session() as conn:
        last = db.kv_get(conn, "last_refresh")
        last_visit = db.kv_get(conn, "last_visit")
        soon = conn.execute(
            "SELECT COUNT(*) c FROM opportunity o JOIN user_state u ON u.opportunity_key=o.dedup_key "
            "WHERE o.status='open' AND (u.favourite=1 OR u.app_status IS NOT NULL) AND o.deadline != '' "
            "AND o.deadline BETWEEN date('now') AND date('now', '+7 day')").fetchone()["c"]
        chats = chatlog.recent(conn)
        chats_total = chatlog.count(conn)
        todo_open = todo.open_count(conn)
        alert = _deadline_alert(conn, request)
    days = None
    if last:
        days = (date.today() - date.fromisoformat(last[:10])).days
    colors = {c.id: c.color for c in p.categories} if not isinstance(p, Exception) else {}
    labels = {c.id: c.label for c in p.categories} if not isinstance(p, Exception) else {}
    tokens = p.css_tokens if not isinstance(p, Exception) else {}
    return {"request": request, "portfolio": p, "last_refresh_days": days, "deadlines_week": soon,
            "last_visit": last_visit, "chats": chats, "chats_total": chats_total, "todo_open": todo_open,
            "deadline_alert": alert, "cat_colors": colors, "cat_labels": labels, "tokens": tokens,
            "progress": pipeline.progress_state(), "prefs_error": config.prefs_error, "sheet_error": prefs().get("_sheet_error"), **kw}


def _deadline_alert(conn, request: Request) -> list[dict]:
    """Saved calls closing within a week, shown in a pop-up on the first page
    you open each day (never inside an htmx update)."""
    if request.headers.get("hx-request"):
        return []
    today = date.today().isoformat()
    if db.kv_get(conn, "deadline_alert_day") == today:
        return []
    rows = todo.closing_soon(conn)
    if rows:
        db.kv_set(conn, "deadline_alert_day", today)
    return rows


def _mark_visit() -> None:
    """Bookkeeping for the 'new since last visit' dots: never fail a page for it."""
    try:
        with db.session() as conn:
            db.kv_set(conn, "prev_visit", db.kv_get(conn, "last_visit"))
            db.kv_set(conn, "last_visit", db.now())
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- filters

FILTERS = report.FILTERS
_where = report.filter_sql


def _filters(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in FILTERS if request.query_params.get(k)}


def _options(conn) -> dict:
    return {
        "types": [r[0] for r in conn.execute("SELECT DISTINCT type FROM opportunity WHERE status='open' AND type IS NOT NULL ORDER BY type")],
        "sources": [r[0] for r in conn.execute("SELECT DISTINCT source_id FROM opportunity WHERE status='open' ORDER BY source_id")],
        "langs": ["en", "it", "nl", "de", "es", "fr"],
    }


# ---------------------------------------------------------------- pages

def _group_where(gid: str) -> tuple[str, list]:
    """SQL for one feed tab (see present.FEED_GROUPS)."""
    for g, _, types in present.FEED_GROUPS:
        if g == gid and types:
            return f" AND o.type IN ({','.join('?' * len(types))})", list(types)
    listed = [t for _, _, types in present.FEED_GROUPS for t in types]
    return f" AND COALESCE(o.type, '') NOT IN ({','.join('?' * len(listed))})", listed


def _last_batch(conn) -> str:
    """Start of the last finished refresh: what it found is 'new' until the next one."""
    row = conn.execute("SELECT started FROM run WHERE status = 'done' ORDER BY id DESC LIMIT 1").fetchone()
    return row["started"] if row else ""


def _feed_tabs(conn, where: str, params: list, since: str = "") -> list[dict]:
    counts = feed_counts(conn, where + " AND COALESCE(m.bucket, 'feed') = 'feed'", tuple(params))
    new = feed_counts(conn, where + " AND COALESCE(m.bucket, 'feed') = 'feed' AND o.first_seen >= ?",
                      tuple(params + [since])) if since else {}
    tabs = []
    for gid, label, types in present.FEED_GROUPS:
        def total(c: dict, key: str) -> int:
            return sum(v[key] for t, v in c.items() if present.feed_group_of(t or None) == gid)
        # mixed tabs label each row with its type; single-type tabs don't need to
        tabs.append({"id": gid, "label": label, "n": total(counts, "n"), "bad": total(counts, "bad"),
                     "new": total(new, "n"), "mixed": len(types) != 1})
    return tabs


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """The chat: ask the app about its opportunities, sources, studios and
    your portfolio (vs/chat.py)."""
    return templates.TemplateResponse(request, "chat.html", ctx(request, view="chat", chat=None))


@app.get("/chat/{cid}", response_class=HTMLResponse)
def chat_page(request: Request, cid: int):
    """A saved chat, to read again or continue."""
    with db.session() as conn:
        c = chatlog.get(conn, cid)
    if not c:
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(request, "chat.html", ctx(request, view="chat", chat=c))


@app.get("/chats", response_class=HTMLResponse)
def chats_page(request: Request):
    with db.session() as conn:
        rows = chatlog.recent(conn, None)
    return templates.TemplateResponse(request, "chats.html", ctx(request, view="chats", all_chats=rows))


@app.post("/chat/{cid}/delete")
def chat_delete(request: Request, cid: int):
    with db.session() as conn:
        chatlog.delete(conn, cid)
    return RedirectResponse("/chats", 303)


@app.post("/chat/ask", response_class=HTMLResponse)
def chat_ask(request: Request, q: str = Form(""), last: str = Form(""), plan: str = Form(""), chat_id: str = Form("")):
    from vs import chat
    p = _portfolio()
    ids = [int(x) for x in last.split(",") if x.strip().isdigit()]
    given = None
    if plan:
        try:
            given = {**chat.EMPTY_PLAN, **json.loads(plan)}
        except ValueError:
            given = None
    with db.session() as conn:
        c = chatlog.get(conn, int(chat_id)) if chat_id.isdigit() else None
        history = [t["q"] for t in (c["turns"] if c else [])]
        a, plan_json = chat.answer(conn, q, p if not isinstance(p, Exception) else None, ids, plan=given, history=history)
        cid = c["id"] if c else chatlog.create(conn, q or a.title)
        turn = len(history)
    # numbers ("the first one") keep pointing at the last results until new ones are shown
    shown = ",".join(str(i) for i in ([r["id"] for r in a.rows] if a.kind == "opportunities" and a.rows else ids))
    # The results come at once; the model's few sentences above them follow (/chat/{cid}/reply/{turn}).
    page = ctx(request, q=q or "", a=a, plan_json=plan_json, new_since="", chat_id=cid, turn=turn,
               reply_url=f"/chat/{cid}/reply/{turn}" if q.strip() else "")
    html = templates.get_template("_chat_answer.html").render(page)
    with db.session() as conn:
        chatlog.add_turn(conn, cid, q or a.title, html, shown, facts=chat.facts(a) if q.strip() else "")
        page.update(chats=chatlog.recent(conn), chats_total=chatlog.count(conn))
    # The answer, plus (out of band) the ids it shows, the chat it belongs to and the side bar's chat list.
    oob = (f'<input type="hidden" name="last" id="chat-last" value="{shown}" hx-swap-oob="true">'
           f'<input type="hidden" name="chat_id" id="chat-id" value="{cid}" hx-swap-oob="true">'
           + templates.get_template("_sb_chats.html").render({**page, "oob": True}))
    return HTMLResponse(html + oob, headers={"HX-Push-Url": f"/chat/{cid}"})


def _check_progress(job_id: str, job: dict, chat_id: int, turn: int) -> str:
    names = html_escape(", ".join(x["name"] for x in job["sites"]))
    doing = (f"Waiting for the refresh to finish, then checking {names}…" if job["state"] == "waiting"
             else f"Reading {names} now… this takes a minute or two per site.")
    return (f'<div class="chat-check" hx-get="/chat/check/{job_id}?chat_id={chat_id}&amp;turn={turn}" hx-trigger="every 3s" '
            f'hx-swap="outerHTML" role="status"><span class="dots"><i></i><i></i><i></i></span> {doing}</div>')


@app.post("/chat/check", response_class=HTMLResponse)
def chat_check(ids: str = Form(""), chat_id: int = Form(0), turn: int = Form(-1)):
    """Read the sites a question named, now (you clicked "Check now")."""
    from vs import chat
    wanted = set(ids.split("|"))
    sites = [s for s in chat.all_sites() if s["id"] in wanted]  # only sites the app knows
    for s in sites:
        s.pop("keys", None)
    if not sites:
        return HTMLResponse('<p class="small muted">That site is no longer in your sources.</p>')
    job_id = pipeline.check_sites(sites)
    html = _check_progress(job_id, pipeline.check_state(job_id), chat_id, turn)
    if chat_id:
        with db.session() as conn:
            chatlog.settle_check(conn, chat_id, turn, html)
    return HTMLResponse(html)


@app.get("/chat/check/{job_id}", response_class=HTMLResponse)
def chat_check_state(request: Request, job_id: str, chat_id: int = 0, turn: int = -1):
    job = pipeline.check_state(job_id)
    if not job:  # the app restarted meanwhile
        html = '<p class="small muted">The check was interrupted when the app restarted. Ask again to check once more.</p>'
    elif job["state"] in ("waiting", "running"):
        return HTMLResponse(_check_progress(job_id, job, chat_id, turn))
    else:
        sids = [s["id"] for s in job["sites"]]
        with db.session() as conn:
            rows = feed_rows(conn, f"AND o.source_id IN ({','.join('?' * len(sids))})", tuple(sids), 10)
            states = {r["source_id"]: r["error"] for r in conn.execute(
                f"SELECT source_id, error FROM source_state WHERE source_id IN ({','.join('?' * len(sids))})", sids)}
        errors = [f"{s['name']}: {states[s['id']][:120]}" for s in job["sites"] if states.get(s["id"])]
        if job["state"] == "failed":
            errors.append(job["error"][:200])
        html = templates.get_template("_chat_checked.html").render(ctx(
            request, rows=rows, names=", ".join(s["name"] for s in job["sites"]), ok=job["state"] == "done" and not errors,
            errors=errors, links=job["sites"], new_since=""))
    if chat_id:
        with db.session() as conn:
            chatlog.settle_check(conn, chat_id, turn, html)
    return HTMLResponse(html)


@app.get("/chat/{cid}/reply/{turn}", response_class=HTMLResponse)
def chat_reply(cid: int, turn: int):
    """The model's short reply to one turn, written from what the app found and
    the chat so far, in the language of the question. Saved with the turn."""
    from vs import chat
    with db.session() as conn:
        c = chatlog.get(conn, cid)
    turns = (c or {}).get("turns") or []
    if not 0 <= turn < len(turns):
        return HTMLResponse("")
    t = turns[turn]
    if t.get("reply") is None:
        earlier = [(x["q"], x.get("reply") or x.get("facts", "").split("\n")[0]) for x in turns[:turn]]
        text = chat.reply(t["q"], earlier, t.get("facts") or "")
        out = chatlog.reply_html(text)
        with db.session() as conn:
            chatlog.set_reply(conn, cid, turn, text, out)
        return HTMLResponse(out)
    return HTMLResponse(chatlog.reply_html(t["reply"]))


@app.post("/chat/do", response_class=HTMLResponse)
def chat_do(request: Request, payload: str = Form("{}"), chat_id: int = Form(0), turn: int = Form(-1)):
    from vs import chat
    try:
        data = json.loads(payload)
        done = "Cancelled: nothing changed." if data.get("action") == "cancel" else chat.do(data)
    except (ValueError, TypeError, AttributeError):
        done = "That didn't work: nothing was changed."
    out = f'<p class="chat-done" role="status">{html_escape(done)}</p>'
    if chat_id:
        with db.session() as conn:
            chatlog.settle(conn, chat_id, turn, out)
    return HTMLResponse(out)


# ---------------------------------------------------------------- to-do

def _briefing() -> set[int]:
    return {oid for oid, j in _brief_jobs.items() if j.get("state") == "running"}


def _todo_ctx(conn) -> dict:
    return {"states": todo.STATES, "kinds": todo.KINDS, "outcomes": todo.OUTCOMES, "doc_labels": present.DOC_LABELS,
            "call_options": todo.call_options(conn)}


@app.get("/todo", response_class=HTMLResponse)
def todo_page(request: Request, view: str = ""):
    """The to-do board (it replaced the Applications page)."""
    with db.session() as conn:
        b = todo.board(conn, _briefing())
        extra = _todo_ctx(conn)
    return templates.TemplateResponse(request, "todo.html", ctx(
        request, view="todo", board=b, deleted_view=view == "deleted", groups=todo.GROUPS,
        group_counts=todo.group_counts(b), **extra))


@app.get("/todo/card", response_class=HTMLResponse)
def todo_card(request: Request, key: str):
    """One card again (after a document is ticked, or while its brief is prepared)."""
    with db.session() as conn:
        c = todo.find(todo.board(conn, _briefing()), key)
        extra = _todo_ctx(conn)
    if not c:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_todo_card.html", ctx(request, c=c, **extra))


@app.post("/todo/doc", response_class=HTMLResponse)
def todo_doc(request: Request, oid: int = Form(...), doc: str = Form(...), on: int = Form(0)):
    with db.session() as conn:
        todo.set_doc(conn, oid, doc, bool(on))
    return todo_card(request, f"call:{oid}")


@app.post("/todo/doc/add")
def todo_doc_add(oid: int = Form(...), kind: str = Form("")):
    with db.session() as conn:
        todo.add_doc(conn, oid, kind)
    return RedirectResponse("/todo", 303)


@app.post("/todo/note")
def todo_note(key: str = Form(...), text: str = Form("")):
    with db.session() as conn:
        oid = todo.set_note(conn, key, text)
    if oid:  # a call's note is the one on its page
        log_signal("note", key=_key(oid), text=text.strip()[:1000])
    return RedirectResponse("/todo", 303)


@app.post("/todo/edit")
def todo_edit(tid: int = Form(...), text: str = Form(""), kind: str = Form("portfolio"), due: str = Form(""), oid: str = Form("")):
    with db.session() as conn:
        todo.edit_own(conn, tid, text, kind, due, oid)
    return RedirectResponse("/todo", 303)


@app.post("/todo/act")
async def todo_act(request: Request):
    """One action on one card (its ⋯ menu) or on every selected card."""
    form = await request.form()
    keys, action = [str(k) for k in form.getlist("keys")], str(form.get("action", ""))
    with db.session() as conn:
        signals = todo.act(conn, keys, action)
    for oid, sig in signals:
        kind, _, value = sig.partition(":")
        if kind == "status":
            log_signal("status", key=_key(int(oid)), status=value if value in STATUSES else "")
        else:
            log_signal(kind, key=_key(int(oid)))
    return RedirectResponse("/todo?view=deleted" if action == "restore" and form.get("from") == "deleted" else "/todo", 303)


@app.post("/todo/add")
def todo_add(request: Request, text: str = Form(""), kind: str = Form("portfolio"), due: str = Form(""), oid: str = Form("")):
    with db.session() as conn:
        todo.add_own(conn, text, kind, due, oid)
    return RedirectResponse("/todo", 303)


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request):
    f = _filters(request)
    f.pop("type", None)  # the tabs replace the type filter on this page
    where, params = _where(f)
    only_new = request.query_params.get("new") == "1"
    with db.session() as conn:
        expiry.expire(conn)  # events whose dates passed since the last refresh
        prev_visit = db.kv_get(conn, "last_visit") or ""
        since = _last_batch(conn)
        tabs = _feed_tabs(conn, where, params, since)
        # Open the tab asked for; otherwise the first one with something in it.
        key = "new" if only_new else "n"
        gid = request.query_params.get("g", "")
        if gid not in {t["id"] for t in tabs}:
            gid = next((t["id"] for t in tabs if t[key]), tabs[0]["id"])
        tab = next(t for t in tabs if t["id"] == gid)
        gw, gp = _group_where(gid)
        base = where + gw + " AND COALESCE(m.bucket, 'feed') = 'feed'"
        # While a refresh runs, what it scores appears on top, one by one.
        live = _live_since()
        live_rows = _live_rows(conn, base, params + gp, live) if live else []
        if live:
            base, gp = base + " AND o.first_seen < ?", gp + [live]
        # What the last refresh found goes in its own area on top; the rest below.
        new_rows = feed_rows(conn, base + " AND o.first_seen >= ?", tuple(params + gp + [since]),
                             present.FEED_PAGE) if since and tab["new"] else []
        rows, total = [], 0
        if not only_new:
            older = " AND o.first_seen < ?" if new_rows else ""
            rows = feed_rows(conn, base + older, tuple(params + gp + ([since] if new_rows else [])), present.FEED_PAGE)
            total = tab["n"] - tab["new"] if new_rows else tab["n"]
        opts = _options(conn)
        hidden_count = conn.execute("SELECT COUNT(*) FROM opportunity WHERE status = 'hidden'").fetchone()[0]
        loosen = _loosen(conn, f, gid, since, only_new) if f and not tab[key] else []
    _mark_visit()
    return templates.TemplateResponse(request, "feed.html", ctx(
        request, rows=rows, new_rows=new_rows, live_rows=live_rows, live=live, only_new=only_new, since=since,
        total=total, older=bool(new_rows), loosen=loosen,
        f=f, opts=opts, view="feed", hidden_count=hidden_count, new_since=prev_visit,
        qs=urlencode(f), tabs=tabs, tab=tab, offset=0, next_offset=len(rows), prev_far=False))


FILTER_NAMES = {"q": "your search", "region": "the region filter", "within": "the deadline filter", "elig": "the eligibility filter",
                "remote": "the on site / remote filter", "money": "the money filter", "lang": "the language filter",
                "source": "the source filter", "status": "the status filter", "nofee": "“No fee”", "housing": "“Housing covered”", "min": "the score filter"}


def _loosen(conn, f: dict, gid: str, since: str, only_new: bool) -> list[tuple[str, int, str]]:
    """When the filters leave a tab empty: for each filter, how many there
    would be without it, so the empty page can offer that as one click."""
    out = []
    for k in f:
        rest = {kk: v for kk, v in f.items() if kk != k}
        where, params = _where(rest)
        tab = next((t for t in _feed_tabs(conn, where, params, since) if t["id"] == gid), None)
        n = (tab["new"] if only_new else tab["n"]) if tab else 0
        if n:
            out.append((f"Remove {FILTER_NAMES.get(k, k)}", n, urlencode(rest | {"g": gid} | ({"new": "1"} if only_new else {}))))
    return sorted(out, key=lambda x: -x[1])


def _live_since() -> str:
    """Start of the running refresh ('' when none runs)."""
    st = pipeline.progress_state()
    return st["started"] if st["running"] and st["kind"] == "refresh" else ""


def _live_rows(conn, base: str, params: list, live: str) -> list[dict]:
    # Only what already has its score: each one appears when the Scorer is done with it.
    return feed_rows(conn, base + " AND o.first_seen >= ? AND m.final IS NOT NULL", tuple(params + [live]), 60)


@app.get("/feed/live", response_class=HTMLResponse)
def feed_live(request: Request, g: str):
    """The 'Arriving now' area, polled while a refresh runs."""
    f = _filters(request)
    f.pop("type", None)
    where, params = _where(f)
    gw, gp = _group_where(g)
    live = _live_since() or request.query_params.get("since", "")
    with db.session() as conn:
        rows = _live_rows(conn, where + gw + " AND COALESCE(m.bucket, 'feed') = 'feed'", params + gp, live) if live else []
    return templates.TemplateResponse(request, "_live.html", ctx(
        request, live_rows=rows, live=live, running=bool(_live_since()), qs=urlencode(f), tab={"id": g}, new_since=""))


@app.get("/feed/more", response_class=HTMLResponse)
def feed_more(request: Request, g: str, offset: int = 0, older: int = 0):
    """The next page of one feed tab, appended in place of the 'Show more' button.
    older=1: only what the last refresh did not find (the new ones are above)."""
    f = _filters(request)
    f.pop("type", None)
    where, params = _where(f)
    gw, gp = _group_where(g)
    with db.session() as conn:
        prev_visit = db.kv_get(conn, "prev_visit") or ""  # the feed already moved last_visit on
        since = _last_batch(conn) if older else ""
        tab = next(t for t in _feed_tabs(conn, where, params, since) if t["id"] == g)
        extra, extra_p = (" AND o.first_seen < ?", [since]) if since else ("", [])
        q, qp = where + gw + " AND COALESCE(m.bucket, 'feed') = 'feed'" + extra, tuple(params + gp + extra_p)
        rows = feed_rows(conn, q, qp, present.FEED_PAGE, max(0, offset))
        # The "far from your work" divider goes where the scores cross the line, also between pages.
        before = feed_rows(conn, q, qp, 1, offset - 1) if offset > 0 else []
        prev_far = bool(before) and before[0]["final"] is not None and before[0]["final"] < present.far_below()
    total = tab["n"] - tab["new"] if since else tab["n"]
    return templates.TemplateResponse(request, "_feed_page.html", ctx(
        request, rows=rows, f=f, qs=urlencode(f), tab=tab, new_since=prev_visit, total=total, older=bool(since),
        offset=offset, next_offset=offset + len(rows), prev_far=prev_far))


@app.get("/discovery", response_class=HTMLResponse)
def discovery(request: Request):
    f = _filters(request)
    where, params = _where(f)
    with db.session() as conn:
        rows = feed_rows(conn, where + " AND m.bucket = 'discovery'", tuple(params), 60)
        opts = _options(conn)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("cluster") or "Other", []).append(r)
    return templates.TemplateResponse(request, "discovery.html", ctx(request, groups=groups, f=f, opts=opts, view="discovery",
                                                            new_since="", qs=urlencode(f)))


def _similar_titles(conn, o: dict) -> list[tuple[str, str]]:
    """(ref, title) for 'Similar to …': titles only, never internal keys. A
    starred opportunity ('opp:<key>') is looked up; the item itself, repeats
    and refs whose title can't be found are left out."""
    from vs.score import example_title
    own = {o.get("dedup_key"), o.get("canonical_url")}
    out, seen = [], set()
    for ref in o.get("similar_examples") or []:
        if ref.startswith("opp:"):
            key = ref[4:]
            if key in own:
                continue
            row = conn.execute("SELECT title_en, title_original FROM opportunity WHERE dedup_key=? OR canonical_url=?", (key, key)).fetchone()
            title = (row["title_en"] or row["title_original"]) if row else ""
        else:
            title = example_title(ref)
            if title == ref:
                title = ""
        if title and title not in seen and title != (o.get("title_en") or o.get("title_original")):
            seen.add(title)
            out.append((ref, title))
    return out


@app.get("/o/{oid}", response_class=HTMLResponse)
def detail(request: Request, oid: int):
    with db.session() as conn:
        rows = feed_rows(conn, "AND o.id = ?", (oid,), 1)
        if not rows:
            o = db.row_dict(conn.execute("SELECT * FROM opportunity WHERE id=?", (oid,)).fetchone())
            if not o:
                raise HTTPException(404)
            rows = [o]
        brief = db.kv_get(conn, f"brief:{oid}")
        o = rows[0]
        similar = _similar_titles(conn, o)
    return templates.TemplateResponse(request, "detail.html", ctx(request, o=o, similar=similar, brief=brief, view="detail",
                                                         statuses=STATUSES, reasons=DISMISS_REASONS,
                                                         brief_job=_brief_jobs.get(oid)))


@app.get("/studios", response_class=HTMLResponse)
def studios_page(request: Request, country: str = "", kind: str = ""):
    from vs import studios as studios_mod
    items = studios_mod.load()
    countries = sorted({s.get("country", "") for s in items})
    kinds = sorted({k for s in items for k in s.get("types", [])})
    if country:
        items = [s for s in items if s.get("country") == country]
    if kind:
        items = [s for s in items if kind in s.get("types", [])]
    groups: dict[str, list] = {}
    for s in sorted(items, key=lambda s: (s.get("country", ""), s.get("city", ""), s["name"])):
        groups.setdefault(s.get("country", "?"), []).append(s)
    return templates.TemplateResponse(request, "studios.html", ctx(
        request, groups=groups, countries=countries, kinds=kinds, sel_country=country, sel_kind=kind,
        statuses=studios_mod.STATUSES, total=len(items), view="studios"))


@app.post("/studios/status")
def studios_status(request: Request, name: str = Form(...), status: str = Form(...)):
    from vs import studios as studios_mod
    studios_mod.set_status(name, status)
    return RedirectResponse(_back(request, "/studios"), 303)


@app.get("/hidden", response_class=HTMLResponse)
def hidden(request: Request):
    """Everything your rules hid, with the reason. Edit the rules in
    preferences.toml ([intake].field_titles, [taste].hide_if) to bring things back."""
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title_en, title_original, org_name, source_id, hidden_reason, canonical_url FROM opportunity "
            "WHERE status = 'hidden' ORDER BY hidden_reason, source_id, title_en")]
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["hidden_reason"] or "?", []).append(r)
    return templates.TemplateResponse(request, "hidden.html", ctx(request, groups=groups, total=len(rows), view="feed"))


@app.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, month: str = "", scope: str = "tracked"):
    today = date.today()
    y, m = (int(x) for x in month.split("-")) if month else (today.year, today.month)
    first = date(y, m, 1)
    last = date(y, m, cal_mod.monthrange(y, m)[1])
    extra = "" if scope == "all" else "AND (u.favourite = 1 OR u.app_status IS NOT NULL)"
    with db.session() as conn:
        rows = feed_rows(conn, f"AND o.deadline BETWEEN ? AND ? {extra} AND COALESCE(u.dismissed,0)=0",
                         (first.isoformat(), last.isoformat()), 400)
        agenda = feed_rows(conn, f"AND o.deadline >= date('now') {extra} AND COALESCE(u.dismissed,0)=0", (), 400)
    agenda.sort(key=lambda r: r["deadline"])
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["deadline"], []).append(r)
    weeks = cal_mod.Calendar(firstweekday=0).monthdatescalendar(y, m)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (last + timedelta(days=1)).strftime("%Y-%m")
    return templates.TemplateResponse(request, "calendar.html", ctx(request, weeks=weeks, by_day=by_day, month=first, today=today,
                                                           prev_m=prev_m, next_m=next_m, scope=scope, agenda=agenda,
                                                           view="calendar"))


@app.get("/calendar.ics")
def calendar_ics(scope: str = "tracked"):
    extra = "" if scope == "all" else "AND (u.favourite = 1 OR u.app_status IS NOT NULL)"
    with db.session() as conn:
        rows = feed_rows(conn, f"AND o.deadline >= date('now') {extra}", (), 1000)
    return Response(to_ics(rows), media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="deadlines-{scope}.ics"'})


@app.get("/o/{oid}.ics")
def one_ics(oid: int):
    with db.session() as conn:
        o = db.row_dict(conn.execute("SELECT * FROM opportunity WHERE id=?", (oid,)).fetchone())
    if not o:
        raise HTTPException(404)
    return Response(to_ics([o]), media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="deadline-{oid}.ics"'})


@app.get("/tracker")
def tracker():
    return RedirectResponse("/todo", 301)  # the Applications page became the to-do board


@app.get("/taste", response_class=HTMLResponse)
def taste(request: Request):
    from vs.taste import summary
    exs = list_examples()
    sigs = [s for s in taste_signals() if s["ref"].startswith("opp:")]
    with db.session() as conn:
        scored = db.kv_get(conn, "last_scored") or {}
        evals = db.kv_get(conn, "eval_history") or []
        taste_sum = db.kv_get(conn, "taste_summary")
        suggestions = db.kv_get(conn, "source_suggestions") or []
        titles = {r["dedup_key"]: (r["id"], r["title_en"]) for r in conn.execute(
            "SELECT id, dedup_key, title_en FROM opportunity WHERE dedup_key IN (%s)" % ",".join("?" * len(sigs)),
            tuple(s["ref"][4:] for s in sigs))} if sigs else {}
    return templates.TemplateResponse(request, "taste.html", ctx(
        request, examples=exs, sigs=sigs, titles=titles, scored=scored, evals=evals, taste_sum=taste_sum,
        stats=summary(), statements=prefs().get("taste", {}).get("statements", []), suggestions=suggestions,
        refs=references.load(), ref_total=prefs().get("weights", {}).get("reference_total", 3.0),
        view="taste", prefill=dict(request.query_params)))


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request):
    """Bookmarklet target: /add?url=...&title=...&sel=..."""
    return templates.TemplateResponse(request, "add.html", ctx(request, view="taste", prefill=dict(request.query_params)))


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    p = prefs()
    with db.session() as conn:
        states = {r["source_id"]: db.row_dict(r) for r in conn.execute("SELECT * FROM source_state")}
        runs = [db.row_dict(r) for r in conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 15")]
        counts = {r["source_id"]: r["c"] for r in conn.execute(
            "SELECT source_id, COUNT(*) c FROM opportunity WHERE status='open' GROUP BY source_id")}
    configured = [(sid, cfg) for sid, cfg in p.get("sources", {}).items()]
    configured += [(f"watch:{w['name']}", {"adapter": "watch", **w}) for w in p.get("watchlist", [])]
    try:
        models = llm.available_models()
        llm_error = None
    except llm.LLMError as e:
        models, llm_error = [], str(e)
    return templates.TemplateResponse(request, "sources.html", ctx(request, configured=configured, states=states, runs=runs,
                                                          counts=counts, view="sources", models=models, llm_error=llm_error))


@app.get("/runs/{rid}", response_class=HTMLResponse)
def run_detail(request: Request, rid: int):
    with db.session() as conn:
        run = db.row_dict(conn.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone())
    if not run:
        raise HTTPException(404)
    report = Path(run["report_path"]).read_text(encoding="utf-8") if run.get("report_path") and Path(run["report_path"]).exists() else ""
    import markdown as md
    return templates.TemplateResponse(request, "run.html", ctx(request, run=run, report_html=md.markdown(report, extensions=["tables"]),
                                                      view="sources"))


@app.get("/files")
def files(path: str):
    """Serve generated outputs (briefs, PDFs, reports) from the app's output folder only."""
    from vs.config import OUTPUT_DIR
    target = Path(path).resolve()
    if OUTPUT_DIR.resolve() not in target.parents or not target.exists():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/site-font/{name:path}")
def site_font(name: str):
    """Fonts are read (never copied) from the website folder."""
    p = _portfolio()
    if isinstance(p, Exception):
        raise HTTPException(404)
    fonts = (p.root / "fonts").resolve()
    target = (fonts / name).resolve()
    if fonts not in target.parents or not target.exists():
        raise HTTPException(404)
    return FileResponse(target)


# ---------------------------------------------------------------- actions

def _back(request: Request, fallback: str = "/") -> str:
    return request.headers.get("referer") or fallback


@app.post("/o/{oid}/fav")
def fav(request: Request, oid: int, on: int = Form(1)):
    key = _key(oid)
    log_signal("favourite" if on else "unfavourite", key=key)
    if request.headers.get("hx-request"):
        return HTMLResponse(present.save_button(oid, bool(on)))
    return RedirectResponse(_back(request), 303)


@app.post("/o/{oid}/dismiss")
def dismiss(request: Request, oid: int, reason: str = Form("other"), undo: int = Form(0)):
    log_signal("undismiss" if undo else "dismiss", key=_key(oid), reason=reason)
    if request.headers.get("hx-request"):
        if undo:  # the card comes back where it was
            with db.session() as conn:
                rows = feed_rows(conn, "AND o.id = ?", (oid,), 1)
            if rows:
                return templates.TemplateResponse(request, "_card.html", ctx(request, o=rows[0], new_since=""))
            return HTMLResponse("<p class='small muted'>Restored: it is back in the feed.</p>")
        reason = reason if reason in dict(DISMISS_REASONS) else "other"
        label = dict(DISMISS_REASONS).get(reason, "Other")
        return HTMLResponse(
            f'<article class="card gone" id="card-{oid}" aria-live="polite"><p>Dismissed: {label}. The feed learns from it.</p>'
            f'<button class="btn" hx-post="/o/{oid}/dismiss" hx-vals=\'{{"undo": 1, "reason": "{reason}"}}\' '
            f'hx-target="#card-{oid}" hx-swap="outerHTML" hx-disabled-elt="this">{icon("undo-2")} Undo</button></article>')
    return RedirectResponse(_back(request), 303)


@app.post("/o/{oid}/status")
def set_status(request: Request, oid: int, status: str = Form("")):
    log_signal("status", key=_key(oid), status=status if status in STATUSES else "")
    return RedirectResponse(_back(request, f"/o/{oid}"), 303)


@app.post("/o/{oid}/note")
def set_note(request: Request, oid: int, text: str = Form("")):
    log_signal("note", key=_key(oid), text=text)
    return RedirectResponse(_back(request, f"/o/{oid}"), 303)


@app.post("/o/{oid}/like")
def like(request: Request, oid: int, label: str = Form("interested"), why: str = Form("")):
    """'More / less like this': turns a feed item into an explicit example."""
    with db.session() as conn:
        o = db.row_dict(conn.execute("SELECT * FROM opportunity WHERE id=?", (oid,)).fetchone())
    from vs.examples import save_example
    save_example(label, o.get("raw_text") or o.get("summary_en") or "", url=o.get("canonical_url") or "", why=why,
                 title=o.get("title_en") or "",
                 extracted={k: o.get(k) for k in ("title_en", "org_name", "type", "field_tag", "summary_en", "deadline",
                                                  "location_country", "remote_mode")})
    return RedirectResponse(_back(request, f"/o/{oid}"), 303)


@app.post("/o/{oid}/brief")
def make_brief(request: Request, oid: int, pdf: int = Form(1), back: str = Form("")):
    job = _brief_jobs.get(oid)
    if not job or job.get("state") != "running":
        _brief_jobs[oid] = {"state": "running", "log": []}

        def run():
            from vs.brief import generate
            try:
                res = generate(oid, make_pdf=bool(pdf), log=lambda m: _brief_jobs[oid]["log"].append(str(m)))
                _brief_jobs[oid].update(state="done", result=res)
            except Exception as e:
                _brief_jobs[oid].update(state="failed", error=str(e))
        threading.Thread(target=run, daemon=True).start()
    return RedirectResponse("/todo" if back == "/todo" else f"/o/{oid}#brief", 303)


@app.get("/o/{oid}/brief-status", response_class=HTMLResponse)
def brief_status(request: Request, oid: int):
    with db.session() as conn:
        brief = db.kv_get(conn, f"brief:{oid}")
    return templates.TemplateResponse(request, "_brief.html", {"request": request, "o": {"id": oid}, "brief": brief,
                                                      "brief_job": _brief_jobs.get(oid)})


@app.post("/refresh")
def refresh(request: Request, force: int = Form(0), source: str = Form("")):
    pipeline.refresh_in_background(force=bool(force), only=source or None)
    return RedirectResponse(_back(request, "/sources"), 303)


@app.post("/stop")
def stop(request: Request):
    pipeline.request_stop()
    return RedirectResponse(_back(request, "/"), 303)


@app.post("/agents/{name}/stop")
def stop_agent(request: Request, name: str):
    """Stop one agent of the running refresh (from the progress panel)."""
    pipeline.stop_agent(name)
    return HTMLResponse("")


RESTARTING_PAGE = """<!doctype html><meta charset="utf-8"><title>Restarting</title>
<body style="font:15px system-ui;padding:40px;background:#f7f7f4;color:#1d1d1b">
<p>Restarting the app&hellip; this page reloads by itself.</p>
<script>
setTimeout(function poll() {
  fetch("/progress", {cache: "no-store"}).then(r => r.ok ? location.replace("/") : setTimeout(poll, 1000))
    .catch(() => setTimeout(poll, 1000));
}, 2500);
</script></body>"""


@app.post("/restart", response_class=HTMLResponse)
def restart(request: Request):
    """Exit so `vs serve` starts the dashboard again with the current code.
    A running refresh stops; what it read is saved and the next refresh continues."""
    from vs.cli import RESTART_CODE
    threading.Timer(1.0, lambda: os._exit(RESTART_CODE)).start()  # after this page is sent
    return HTMLResponse(RESTARTING_PAGE)


@app.post("/rescore")
def rescore(request: Request):
    threading.Thread(target=lambda: pipeline._safe(pipeline.rescore), daemon=True).start()
    return RedirectResponse(_back(request, "/"), 303)


@app.get("/progress", response_class=HTMLResponse)
def progress(request: Request):
    return templates.TemplateResponse(request, "_progress.html", {"request": request, "progress": pipeline.progress_state()})


@app.post("/examples")
def add_example(request: Request, label: str = Form(...), url: str = Form(""), text: str = Form(""),
                why: str = Form(""), title: str = Form("")):
    if not url.strip() and not text.strip():
        raise HTTPException(400, "Give a URL or paste the text")

    def run():
        try:
            res = pipeline.add_example(label, url=url, text=text, why=why, title=title)
            with db.session() as conn:
                db.kv_set(conn, "last_example_result", res)
            from vs.taste import suggest_sources
            suggest_sources()
        except Exception as e:
            with db.session() as conn:
                db.kv_set(conn, "last_example_result", {"status": "failed", "error": str(e)})
    threading.Thread(target=run, daemon=True).start()
    return RedirectResponse("/taste?added=1", 303)


@app.post("/examples/{eid}/remove")
def rm_example(eid: str):
    remove_example(eid)
    return RedirectResponse("/taste", 303)


@app.post("/taste/statements")
def statements(request: Request, text: str = Form(...)):
    lines = [line.strip("-• ").strip() for line in text.splitlines() if line.strip()]

    def mutate(doc):
        doc["taste"]["statements"] = lines
    update_prefs(mutate)
    return RedirectResponse("/taste?saved=corrections#corrections", 303)


@app.post("/taste/summary")
def taste_summary(request: Request):
    from vs.taste import llm_summary
    threading.Thread(target=lambda: pipeline._safe(llm_summary), daemon=True).start()
    return RedirectResponse("/taste", 303)


@app.post("/taste/eval")
def run_eval(request: Request):
    def both():
        from vs.evaluate import evaluate
        from vs.evaluate_stars import run as stars_run
        res = stars_run({"current weights": {}})
        with db.session() as conn:
            hist = db.kv_get(conn, "star_eval_history") or []
            db.kv_set(conn, "star_eval_history", (hist + [{"at": db.now(), **res}])[-30:])
        evaluate()
    threading.Thread(target=lambda: pipeline._safe(both), daemon=True).start()
    return RedirectResponse("/taste", 303)


@app.post("/references/update")
def references_update(request: Request):
    """Read the studios' project pages (a few minutes; runs in the background)."""
    threading.Thread(target=lambda: pipeline._safe(references.update), daemon=True).start()
    return RedirectResponse("/taste?saved=references#references", 303)


@app.post("/references/remove")
def references_remove(request: Request, ref_id: str = Form(...)):
    references.remove(ref_id)
    return RedirectResponse("/taste#references", 303)


@app.post("/sources/suggestion")
def suggestion(request: Request, url: str = Form(...), name: str = Form(""), action: str = Form("accept")):
    from vs.taste import resolve_suggestion
    resolve_suggestion(url, name, accept=(action == "accept"))
    return RedirectResponse("/taste#suggestions", 303)


def _key(oid: int) -> str:
    with db.session() as conn:
        row = conn.execute("SELECT dedup_key FROM opportunity WHERE id=?", (oid,)).fetchone()
    if not row:
        raise HTTPException(404)
    return row["dedup_key"]
