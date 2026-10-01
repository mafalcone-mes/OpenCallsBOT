"""Local dashboard: FastAPI + Jinja2 + HTMX. Run with `uv run vs serve`."""

from __future__ import annotations

import calendar as cal_mod
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from vs import db, llm, pipeline, present, references
from vs import portfolio as portfolio_mod
from vs import config
from vs.config import prefs, update_prefs
from vs.examples import list_examples, log_signal, remove_example, taste_signals
from vs.ics import to_ics
from vs.report import feed_rows

HERE = Path(__file__).parent
app = FastAPI(title="vacancies searcher")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(present=present)

STATUSES = ["shortlisted", "preparing", "submitted", "selected", "rejected", "no_answer"]
DISMISS_REASONS = [("not_my_field", "Not my field"), ("not_my_taste", "Not my taste"), ("too_corporate", "Too corporate"),
                   ("wrong_seniority", "Wrong seniority"), ("timing", "Bad timing"), ("location", "Location"),
                   ("money", "Money"), ("ineligible", "Not eligible"), ("duplicate", "Duplicate")]
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
    days = None
    if last:
        days = (date.today() - date.fromisoformat(last[:10])).days
    colors = {c.id: c.color for c in p.categories} if not isinstance(p, Exception) else {}
    labels = {c.id: c.label for c in p.categories} if not isinstance(p, Exception) else {}
    tokens = p.css_tokens if not isinstance(p, Exception) else {}
    return {"request": request, "portfolio": p, "last_refresh_days": days, "deadlines_week": soon,
            "last_visit": last_visit, "cat_colors": colors, "cat_labels": labels, "tokens": tokens,
            "progress": pipeline.progress_state(), "prefs_error": config.prefs_error, "sheet_error": prefs().get("_sheet_error"), **kw}


def _mark_visit() -> None:
    """Bookkeeping for the 'new since last visit' dots: never fail a page for it."""
    try:
        with db.session() as conn:
            db.kv_set(conn, "prev_visit", db.kv_get(conn, "last_visit"))
            db.kv_set(conn, "last_visit", db.now())
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- filters

FILTERS = ["type", "region", "remote", "money", "nofee", "housing", "within", "lang", "elig", "source", "status", "min", "q"]


def _where(f: dict) -> tuple[str, list]:
    where, params = [], []
    if f.get("type"):
        where.append("o.type = ?"); params.append(f["type"])
    if f.get("region"):
        where.append("o.region = ?"); params.append(f["region"])
    if f.get("remote"):
        where.append("o.remote_mode IN ('remote','online')" if f["remote"] == "yes" else "o.remote_mode NOT IN ('remote','online')")
    if f.get("money"):
        where.append("EXISTS (SELECT 1 FROM json_each(o.conditions, '$.money_kind') WHERE value = ?)")
        params.append(f["money"])
    if f.get("nofee"):
        where.append("NOT EXISTS (SELECT 1 FROM json_each(o.conditions, '$.money_kind') WHERE value = 'fee_required') "
                     "AND COALESCE(json_extract(o.conditions, '$.application_fee'), 0) = 0")
    if f.get("housing"):
        where.append("EXISTS (SELECT 1 FROM json_each(o.conditions, '$.covers') WHERE value = 'housing')")
    if f.get("within"):
        where.append("o.deadline != '' AND o.deadline <= date('now', ?)"); params.append(f"+{int(f['within'])} day")
    if f.get("lang"):
        where.append("EXISTS (SELECT 1 FROM json_each(o.application_languages) WHERE lower(value) = ?)")
        params.append(f["lang"])
    if f.get("elig"):
        where.append("m.eligibility_flag = ?"); params.append(f["elig"])
    if f.get("source"):
        where.append("o.source_id = ?"); params.append(f["source"])
    if f.get("status") == "favourite":
        where.append("u.favourite = 1")
    elif f.get("status") == "dismissed":
        where.append("u.dismissed = 1")
    elif f.get("status") == "tracked":
        where.append("u.app_status IS NOT NULL")
    if f.get("status") != "dismissed":
        where.append("COALESCE(u.dismissed, 0) = 0")
    if f.get("min"):
        where.append("m.final >= ?"); params.append(float(f["min"]) / 100)
    if f.get("q"):
        where.append("(o.title_en LIKE ? OR o.org_name LIKE ? OR o.summary_en LIKE ?)")
        params += [f"%{f['q']}%"] * 3
    return (" AND " + " AND ".join(where)) if where else "", params


def _filters(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in FILTERS if request.query_params.get(k)}


def _options(conn) -> dict:
    return {
        "types": [r[0] for r in conn.execute("SELECT DISTINCT type FROM opportunity WHERE status='open' AND type IS NOT NULL ORDER BY type")],
        "sources": [r[0] for r in conn.execute("SELECT DISTINCT source_id FROM opportunity WHERE status='open' ORDER BY source_id")],
        "langs": ["en", "it", "nl", "de", "es", "fr"],
    }


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def feed(request: Request):
    f = _filters(request)
    where, params = _where(f)
    with db.session() as conn:
        prev_visit = db.kv_get(conn, "last_visit") or ""
        rows = feed_rows(conn, where + " AND COALESCE(m.bucket, 'feed') = 'feed'", tuple(params), 200)
        opts = _options(conn)
        hidden_count = conn.execute("SELECT COUNT(*) FROM opportunity WHERE status = 'hidden'").fetchone()[0]
    _mark_visit()
    return templates.TemplateResponse(request, "feed.html", ctx(request, rows=rows, f=f, opts=opts, view="feed", hidden_count=hidden_count,
                                                       new_since=prev_visit, qs=urlencode(f)))


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
    from vs.score import example_title
    similar = [(ref, example_title(ref)) for ref in (o.get("similar_examples") or [])]
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


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request):
    with db.session() as conn:
        rows = [db.row_dict(r) for r in conn.execute(
            """SELECT o.*, u.app_status, u.notes, u.favourite, m.final FROM opportunity o
               JOIN user_state u ON u.opportunity_key = o.dedup_key LEFT JOIN match m ON m.opportunity_id = o.id
               WHERE u.app_status IS NOT NULL OR u.favourite = 1 ORDER BY o.deadline""")]
    cols = {"favourite": [r for r in rows if not r["app_status"]]}
    for s in STATUSES:
        cols[s] = [r for r in rows if r["app_status"] == s]
    return templates.TemplateResponse(request, "tracker.html", ctx(request, cols=cols, view="tracker", statuses=STATUSES))


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
        return HTMLResponse(_fav_button(oid, bool(on)))
    return RedirectResponse(_back(request), 303)


def _fav_button(oid: int, on: bool) -> str:
    return (f'<button class="fav {"on" if on else ""}" hx-post="/o/{oid}/fav" hx-vals=\'{{"on": {0 if on else 1}}}\' '
            f'hx-swap="outerHTML" title="{"Remove favourite" if on else "Favourite"}">{"★" if on else "☆"}</button>')


@app.post("/o/{oid}/dismiss")
def dismiss(request: Request, oid: int, reason: str = Form("other"), undo: int = Form(0)):
    log_signal("undismiss" if undo else "dismiss", key=_key(oid), reason=reason)
    if request.headers.get("hx-request"):
        return HTMLResponse("" if not undo else "<p>Restored.</p>")
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
def make_brief(request: Request, oid: int, pdf: int = Form(1)):
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
    return RedirectResponse(f"/o/{oid}#brief", 303)


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
