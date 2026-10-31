"""Refresh: four agents (vs/agents.py) list every source, download the pages,
read them with the LLM and score each one as it is read; then dedupe, expire
and score everything once more. Also the flow for adding an example."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import trafilatura
from rapidfuzz import fuzz

from vs import db, embed, expiry, extract, llm
from vs import portfolio as portfolio_mod
from vs.config import prefs
from vs.examples import save_example, write_example
from vs.fetch import RawItem
from vs.fetch.polite import Blocked, PoliteClient, canonical_url, domain


# ---------------------------------------------------------------- progress

_state = {"running": False, "kind": None, "stopping": False, "run_id": None, "lines": [], "started": None, "agents": []}
_lock = threading.Lock()


def progress_state() -> dict:
    with _lock:
        team = list(_state.get("agents") or [])
        out = {**_state, "lines": list(_state["lines"][-12:])}
    out["agents"] = [a.snapshot() for a in team]
    return out


def request_stop() -> bool:
    """Ask a running refresh to stop reading pages and score what it has."""
    with _lock:
        if _state["kind"] != "refresh" or not _state["running"] or _state["stopping"]:
            return False
        _state["stopping"] = True
        team = list(_state.get("agents") or [])
    _say("Stop requested: finishing the current page, then scoring what was read")
    for a in team:
        a.stop()
    return True


def stop_agent(name: str) -> bool:
    """Stop one agent of a running refresh (and the ones feeding it)."""
    from vs import agents as agents_mod

    with _lock:
        team = list(_state.get("agents") or []) if _state["running"] and _state["kind"] == "refresh" else []
    if not agents_mod.stop(team, name):
        return False
    label = next(a.label for a in team if a.name == name)
    _say(f"{label} stopped" + ("; what it skipped is scored at the end" if name == "scorer"
                               else ", and the agents before it; what is already queued is finished"))
    return True


def _stopping() -> bool:
    with _lock:
        return _state["stopping"]


_flushed = {"at": 0.0}


def _say(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _state["lines"].append(f"{stamp}  {msg}")
        flush = _state["kind"] == "refresh" and _state["running"] and time.monotonic() - _flushed["at"] > 60
        run_id, lines = _state["run_id"], list(_state["lines"])
    print(f"{stamp}  {msg}", flush=True)
    if flush and run_id:
        # Saved as the run goes, so an interrupted run can still be timed.
        # Best effort and never waiting: the caller may itself hold the write
        # lock (uncommitted changes), and waiting for it stalled every page
        # for the full 2-minute timeout.
        _flushed["at"] = time.monotonic()
        try:
            with db.session(timeout=0.5) as conn:
                conn.execute("UPDATE run SET log=? WHERE id=?", ("\n".join(lines), run_id))
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------- language

_detector = None


def detect_lang(text: str) -> str:
    global _detector
    if _detector is None:
        from lingua import Language, LanguageDetectorBuilder
        langs = [Language.ENGLISH, Language.ITALIAN, Language.DUTCH, Language.GERMAN, Language.SPANISH,
                 Language.FRENCH, Language.PORTUGUESE]
        _detector = LanguageDetectorBuilder.from_languages(*langs).build()
    lang = _detector.detect_language_of(text[:3000]) if text.strip() else None
    return lang.iso_code_639_1.name.lower() if lang else ""


# ---------------------------------------------------------------- helpers

def content_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()


def _taleo_text(html: str) -> str:
    """Taleo job pages (WHO, FAO...) render the posting with JavaScript from a
    hidden 'initialHistory' field; decode it to plain text."""
    from urllib.parse import unquote

    from vs.fetch.adapters import strip_html

    m = re.search(r'(?:id|name)="initialHistory"[^>]*value="([^"]*)"', html)
    if not m:
        return ""
    parts = []
    for part in unquote(m.group(1)).split("!|!"):
        text = strip_html(unquote(part)).replace("\\:", ":").replace("!*!", "").strip()
        if len(text) > 40 and not text.startswith("ftlUtil_") and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)


NOISE = re.compile(r"menu|nav|footer|header|sidebar|related|recommend|deadline-list|share|cookie|newsletter|breadcrumb", re.I)
KEY_CLASS = re.compile(r"deadline|date|fee|salary|eligib|duration|location|scadenza|termin", re.I)
APPLY_TEXT = re.compile(r"apply|application|call|more info|website|official|candidat|bando|aanmeld|bewerb|solicit|inscri", re.I)


def page_text(html: str, url: str = "") -> str:
    """Main text (trafilatura) plus a 'details' block with the key/value
    fields main-text extraction drops: definition lists, tables, dated or
    deadline-classed elements, and outbound 'apply' links. Menus, sidebars
    and related-item lists are skipped so other calls' dates don't leak in."""
    from selectolax.parser import HTMLParser

    taleo = _taleo_text(html)
    if taleo:
        return taleo
    main = (trafilatura.extract(html, include_tables=True, include_links=False, favor_recall=True) or "").strip()
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript, nav, header, footer, aside, form, svg"):
        node.decompose()
    for node in tree.css("[class], [id]"):
        marker = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if NOISE.search(marker) and node.tag not in ("html", "body", "main", "article"):
            node.decompose()

    details: list[str] = []

    def add(line: str) -> None:
        line = re.sub(r"\s+", " ", line).strip(" -:")
        if 3 < len(line) < 400 and line not in details and line not in main:
            details.append(line)

    for dt in tree.css("dt"):
        dd = dt.next
        while dd is not None and dd.tag != "dd":
            dd = dd.next
        if dd is not None:
            add(f"{dt.text(strip=True)}: {dd.text(separator=' ', strip=True)}")
    for tr in tree.css("tr"):
        cells = [c.text(separator=" ", strip=True) for c in tr.css("th, td")]
        if len(cells) == 2:
            add(f"{cells[0]}: {cells[1]}")
    for node in tree.css("[class]"):
        if KEY_CLASS.search(node.attributes.get("class") or "") and len(node.text(strip=True)) < 200:
            add(node.text(separator=" ", strip=True))
    for t in tree.css("time[datetime]"):
        parent = t.parent
        add(parent.text(separator=" ", strip=True) if parent is not None else t.text(strip=True))
    host = domain(url) if url else ""
    links = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        label = a.text(strip=True)
        if href.startswith("http") and host and domain(href) != host and (APPLY_TEXT.search(label) or APPLY_TEXT.search(href)):
            links.append(f"{label or 'link'}: {href}")
    for line in links[:5]:
        add(f"Link: {line}")
    if details:
        main += "\n\n--- Page details ---\n" + "\n".join(details[:60])
    return main.strip()


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _region(country: str) -> str:
    eu = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "LV", "LT", "LU",
          "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "NO", "CH", "IS", "LI", "GB"}
    c = (country or "").upper()
    return "IT" if c == "IT" else "EU" if c in eu else "world" if c else ""


def build_row(item: RawItem, data: dict, checks: dict, text: str, lang: str) -> dict:
    hints = item.hints or {}
    deadline = data.get("deadline") or hints.get("deadline") or ""
    conditions = {
        "money_kind": data.get("money_kind") or ["unknown"],
        "amount": data.get("amount"), "currency": data.get("currency") or "",
        "period": data.get("amount_period") or "", "covers": data.get("covers") or [],
        "application_fee": data.get("application_fee"),
        "evidence": data.get("money_evidence") or "",
        "for_organisations": bool(hints.get("for_organisations")),
    }
    eligibility = {k: data.get(k) for k in ("age_min", "age_max", "nationalities", "residence", "education", "career_stage")}
    eligibility["evidence"] = data.get("eligibility_evidence") or ""
    status = expiry.status_for({**data, "deadline": deadline})
    return {
        "type": data.get("type") or "other",
        "title_original": item.title, "title_en": data.get("title_en") or item.title,
        "org_name": data.get("org_name") or hints.get("org_name") or "",
        "org_domain": domain(item.url),
        "summary_en": data.get("summary_en") or "", "field_tag": data.get("field_tag") or "",
        "lang_original": lang, "application_languages": data.get("application_languages") or [],
        "location_city": data.get("location_city") or "", "location_country": (data.get("location_country") or "").upper()[:2],
        "region": _region(data.get("location_country") or ""),
        "remote_mode": data.get("remote_mode") or "unknown",
        "deadline": deadline[:10], "deadline_kind": data.get("deadline_kind") or ("fixed" if deadline else "unknown"),
        "start_date": data.get("start_date") or "", "duration_text": data.get("duration_text") or "",
        "conditions": conditions, "eligibility": eligibility,
        "required_docs": data.get("required_docs") or [],
        "submission": {"method": data.get("submission_method") or "unknown", "url": data.get("submission_url") or "",
                       "anonymous": bool(data.get("anonymous_submission"))},
        "seniority": data.get("seniority") or "unknown",
        "other_dates": data.get("other_dates") or [],
        "field_confidence": checks, "raw_text": text[:20000], "status": status,
    }


def upsert(conn, key: str, item: RawItem, row: dict, chash: str, from_example: str | None = None) -> int:
    existing = conn.execute("SELECT id, source_urls, first_seen FROM opportunity WHERE dedup_key=?", (key,)).fetchone()
    cols = dict(row)
    cols.update({"canonical_url": canonical_url(item.url), "source_id": item.source_id, "content_hash": chash,
                 "extraction_status": "done", "last_seen": db.now(), "extract_version": extract.EXTRACT_VERSION})
    if from_example:
        cols["from_example"] = from_example
    for k, v in list(cols.items()):
        if isinstance(v, (dict, list)):
            cols[k] = db.dumps(v)
    if existing:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE opportunity SET {sets} WHERE id=?", (*cols.values(), existing["id"]))
        return existing["id"]
    cols.update({"dedup_key": key, "first_seen": db.now(), "source_urls": db.dumps([item.url])})
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO opportunity({names}) VALUES({marks})", tuple(cols.values()))
    return cur.lastrowid


def find_duplicate(conn, row: dict, vec: np.ndarray | None, model: str, exclude_key: str) -> dict | None:
    """While reading: the same call already stored (same organisation and
    practically the same title; see vs/dedupe.py). Cross-site matches that
    need embeddings are found later, in merge_near_duplicates."""
    from vs.dedupe import why_same

    if not row.get("org_name"):
        return None
    candidates = conn.execute(
        "SELECT id, dedup_key, title_en, org_name, deadline, location_city, source_urls FROM opportunity "
        "WHERE dedup_key != ? AND status IN ('open', 'expired') AND extraction_status = 'done'", (exclude_key,)).fetchall()
    for c in candidates:
        if why_same(row, dict(c)):
            return dict(c)
    return None


# ---------------------------------------------------------------- items

@dataclass
class Prepared:
    """A page downloaded and turned into text, waiting for the Reader."""
    item: RawItem
    text: str
    chash: str
    existing: dict | None
    stale_reject: bool


def prepare_item(client: PoliteClient, conn, item: RawItem) -> str | Prepared:
    """The Downloader's part: everything before the model. Returns a result
    (unchanged | too_short | failed) when the page needs no reading."""
    key = canonical_url(item.url)
    existing = db.row_dict(conn.execute(
        "SELECT id, content_hash, extraction_status, source_urls, status, extract_version FROM opportunity WHERE dedup_key=?",
        (key,)).fetchone())
    # Items rejected as "not a call" under an older prompt are read again.
    stale_reject = bool(existing) and existing["status"] == "not_opportunity" and existing["extract_version"] != extract.EXTRACT_VERSION
    if existing and existing["extraction_status"] == "done" and not item.text and not stale_reject:
        conn.execute("UPDATE opportunity SET last_seen=? WHERE id=?", (db.now(), existing["id"]))
        return "unchanged"
    text = item.text
    if item.needs_detail or not text:
        try:
            r = client.get(item.url, max_age=timedelta(days=7))
            if r.status_code >= 400:
                return "failed"
            text = page_text(r.text, item.url) or item.snippet
        except Blocked:
            text = item.snippet
        except Exception:
            return "failed"
    if len(text) < 120:
        return "too_short"
    chash = content_hash(text)
    if existing and existing["content_hash"] == chash and existing["extraction_status"] == "done" and not stale_reject:
        conn.execute("UPDATE opportunity SET last_seen=? WHERE id=?", (db.now(), existing["id"]))
        return "unchanged"
    return Prepared(item, text, chash, existing, stale_reject)


def read_item(conn, prepared: Prepared, model: str) -> tuple[str, int | None]:
    """The Reader's part: the model reads the page. Returns (new | updated |
    skipped | duplicate, opportunity id)."""
    item, text, chash, existing = prepared.item, prepared.text, prepared.chash, prepared.existing
    key = canonical_url(item.url)
    lang = detect_lang(text)
    data = extract.extract(text, title=item.title, url=item.url, lang=lang)
    if not data.get("is_opportunity", True):
        conn.execute(
            "INSERT INTO opportunity(dedup_key, canonical_url, source_id, title_original, content_hash, extraction_status, status, first_seen, last_seen, extract_version) "
            "VALUES(?,?,?,?,?,'done','not_opportunity',?,?,?) ON CONFLICT(dedup_key) DO UPDATE SET content_hash=excluded.content_hash, "
            "status='not_opportunity', last_seen=excluded.last_seen, extract_version=excluded.extract_version",
            (key, key, item.source_id, item.title, chash, db.now(), db.now(), extract.EXTRACT_VERSION))
        return "skipped", None
    checks = extract.verify(data, text, lang)
    row = build_row(item, data, checks, text, lang)
    # Cross-site duplicates that need embeddings are found at the end of the
    # refresh, in one batch (merge_near_duplicates).
    dup = find_duplicate(conn, row, None, model, key)
    if dup:
        _merge_into(conn, dup, [item.url])
        return "duplicate", None
    opp_id = upsert(conn, key, item, row, chash)
    return ("updated" if existing else "new"), opp_id


def _merge_into(conn, keep: dict, urls: list[str]) -> None:
    merged = set(json.loads(keep["source_urls"] or "[]")) | set(urls)
    conn.execute("UPDATE opportunity SET source_urls=?, last_seen=? WHERE id=?", (db.dumps(sorted(merged)), db.now(), keep["id"]))


def merge_near_duplicates(conn, model: str) -> int:
    """Batch-embed opportunities, re-check earlier merges, then merge the same
    call listed twice (rules and their reasons in vs/dedupe.py). Returns the
    number of new merges."""
    from vs.dedupe import why_same

    opps = [db.row_dict(r) for r in conn.execute(
        "SELECT * FROM opportunity WHERE status IN ('open', 'expired', 'duplicate') AND extraction_status='done' ORDER BY first_seen")]
    vecs = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o) for o in opps}, model)
    cos = lambda x, y: float(vecs[x["dedup_key"]] @ vecs[y["dedup_key"]])  # noqa: E731

    # 1. Re-check earlier merges with today's rules; restore any that don't hold.
    by_url = {}
    for o in opps:
        if o["status"] != "duplicate":
            for u in o.get("source_urls") or []:
                by_url[canonical_url(u)] = o
    restored = 0
    for d in [o for o in opps if o["status"] == "duplicate"]:
        keeper = by_url.get(d["dedup_key"])
        if keeper and why_same(keeper, d, cos(keeper, d)):
            continue
        status = expiry.status_for(d)
        conn.execute("UPDATE opportunity SET status=? WHERE id=?", (status, d["id"]))
        if keeper:
            urls = [u for u in keeper.get("source_urls") or [] if canonical_url(u) != d["dedup_key"]]
            conn.execute("UPDATE opportunity SET source_urls=? WHERE id=?", (db.dumps(urls), keeper["id"]))
            keeper["source_urls"] = urls
        d["status"] = status
        restored += 1
    if restored:
        _say(f"Restored {restored} listing{'s' if restored != 1 else ''} that an older rule had merged by mistake")

    # 2. Merge listings that are the same call.
    live = [o for o in opps if o["status"] in ("open", "expired")]
    merged, gone = 0, set()
    for i, a in enumerate(live):
        if a["id"] in gone:
            continue
        for b in live[i + 1:]:
            if b["id"] in gone:
                continue
            reason = why_same(a, b, cos(a, b))
            if reason:
                _merge_into(conn, {"id": a["id"], "source_urls": db.dumps(a.get("source_urls") or [a["canonical_url"]])},
                            b.get("source_urls") or [b["canonical_url"]])
                a["source_urls"] = sorted(set((a.get("source_urls") or [a["canonical_url"]]) + (b.get("source_urls") or [b["canonical_url"]])))
                conn.execute("UPDATE opportunity SET status='duplicate' WHERE id=?", (b["id"],))
                gone.add(b["id"])
                merged += 1
    if merged:
        _say(f"Merged {merged} duplicate listing{'s' if merged != 1 else ''}")
    return merged


def prefilter(conn, portfolio, items: list[RawItem], keep: int, model: str) -> list[RawItem]:
    """Keep the `keep` items whose title+snippet is closest to the portfolio
    angles or liked examples, without the slow model. Skips known URLs."""
    fresh, seen = [], []
    for it in items:
        key = canonical_url(it.url)
        row = conn.execute("SELECT extraction_status FROM opportunity WHERE dedup_key=?", (key,)).fetchone()
        if not row or row["extraction_status"] != "done" or it.source_id.startswith("watch:"):
            fresh.append(it)  # new, or a watched page that changed since last time
        else:
            seen.append((db.now(), key))
    conn.executemany("UPDATE opportunity SET last_seen=? WHERE dedup_key=?", seen)
    if len(fresh) <= keep:
        return fresh
    angles = embed.angle_vectors(conn, portfolio, model)
    _, a_mat = embed.matrix(angles)
    ex = db.all_vecs(conn, "example", model)
    ref = np.vstack([a_mat] + list(ex.values())) if ex else a_mat
    vecs = llm.embed([f"{it.title}. {it.snippet}" for it in fresh], model=model)
    # Keywords lead (calls are written in those terms); similarity to your
    # projects and examples breaks ties.
    from vs.score import keyword_score
    taste = prefs().get("taste", {})
    scores = [keyword_score(f"{it.title} {it.snippet}", taste)[0] + 0.5 * float((ref @ v).max())
              for it, v in zip(fresh, vecs)]
    order = np.argsort(scores)[::-1][:keep]
    return [fresh[i] for i in order]


def _took(since: float) -> str:
    sec = int(time.monotonic() - since)
    return f"{sec // 3600} h {sec % 3600 // 60} min" if sec >= 3600 else f"{sec // 60} min {sec % 60} s"


# ---------------------------------------------------------------- refresh

def refresh(*, force: bool = False, only: str | list[str] | None = None, rerank: bool = True,
            extra_jobs: list[tuple[str, dict]] | tuple = ()) -> dict:
    """Four agents work at the same time (vs/agents.py): each opportunity is
    listed, downloaded, read and scored in turn, and shows up in the feed as
    soon as it has its score. At the end: duplicates, rules, expiry, and one
    scoring pass over everything (discovery, explanations gone out of date).
    `only`: just these sources; `extra_jobs`: pages read once (a studio's jobs
    page a chat question asked about). A partial run doesn't count as the
    last refresh."""
    from vs import agents as agents_mod

    with _lock:
        if _state["running"]:
            raise RuntimeError("A refresh is already running")
        _state.update(running=True, kind="refresh", stopping=False, lines=[], started=db.now(), agents=[])
    with db.session() as conn:
        # A run left 'running' by a killed process was interrupted; items it
        # read are already saved, so the next refresh simply continues.
        conn.execute("UPDATE run SET status='interrupted', ended=? WHERE status='running'", (db.now(),))
        run_id = conn.execute("INSERT INTO run(started, status) VALUES(?, 'running')", (db.now(),)).lastrowid
    _state["run_id"] = run_id
    client = PoliteClient()
    try:
        portfolio = portfolio_mod.load()
        _say(f"Portfolio: {len(portfolio.projects)} projects, {len(portfolio.angles)} angles (hash {portfolio.profile_hash[:8]})")
        model = llm.embed_model()
        llm.chat_model()  # fail early if no chat model
        _say(f"Models: pages read with {llm.extract_model()}, matches explained with {llm.chat_model()}")
        p = prefs()

        jobs: list[tuple[str, dict]] = [(sid, cfg) for sid, cfg in p.get("sources", {}).items() if cfg.get("enabled", True)]
        jobs += [(f"watch:{w['name']}", {"adapter": "watch", **w}) for w in p.get("watchlist", [])]
        if only is not None or extra_jobs:
            wanted = {only} if isinstance(only, str) else set(only or [])
            jobs = [j for j in jobs if j[0] in wanted] + list(extra_jobs)
        # Least recently completed first: when the page budget runs out, the
        # same sources don't wait at the back every time.
        with db.session() as conn:
            last_ok = {r["source_id"]: r["last_ok"] or "" for r in conn.execute("SELECT source_id, last_ok FROM source_state")}
        jobs.sort(key=lambda j: last_ok.get(j[0], ""))

        phase = time.monotonic()
        llm.take_stats()
        llm.embed_options = {"num_gpu": 0}  # embeddings on the CPU: the chat model keeps the GPU
        run = agents_mod.Run(client=client, portfolio=portfolio, model=model, prefs=p, force=force, rerank=rerank,
                             say=_say, jobs=jobs)
        team = agents_mod.build(run)
        with _lock:
            _state["agents"] = team
        _say("Agents at work: " + ", ".join(f"{a.label} ({a.does})" for a in team))
        for a in team:
            a.start()
        for a in team:
            a.finished.wait()
        counts, sources_report = run.counts, run.sources
        if any(a.stopped() for a in team):
            _say("Stopped: what was read is kept, the rest is read at the next refresh")
        for purpose, s in llm.take_stats().items():
            _say(llm.describe_stats(purpose, s))
        _say(f"Listing, reading and scoring took {_took(phase)}")

        # At the end: embed in one batch, merge cross-listed calls, expire, score.
        phase = time.monotonic()
        _say("Comparing with your portfolio")
        with db.session() as conn:
            counts["duplicate"] += merge_near_duplicates(conn, model)
            from vs import rules as rules_mod
            changes = rules_mod.apply(conn)
            if changes["hidden"] or changes["restored"]:
                _say(f"Your rules: {len(changes['hidden'])} hidden, {len(changes['restored'])} restored")
            conn.commit()
            counts["expired"] = expiry.expire(conn)
            conn.commit()
            from vs import housekeeping
            cleaned = housekeeping.clean(conn, p)
            counts["removed"] = cleaned["removed"]
            conn.commit()
            _say(housekeeping.describe(cleaned))
            _say("Scoring everything again (discovery, explanations gone out of date)")
            stale = p.get("weights", {}).get("rerank_stale_per_run", 10)
            score_counts = score_mod().score_all(conn, portfolio, rerank=rerank, progress=_say,
                                                 stale_limit=None if stale in (None, "") or int(stale) < 0 else int(stale))
            counts.update({f"score_{k}": v for k, v in score_counts.items()})
        for purpose, s in llm.take_stats().items():
            _say(llm.describe_stats(purpose, s))
        _say(f"Comparing and scoring took {_took(phase)}")

        from vs.report import write_run_report
        report_path = write_run_report(run_id, counts, sources_report)
        stopped = _stopping() or any(a.stopped() for a in team)
        with db.session() as conn:
            conn.execute("UPDATE run SET ended=?, status=?, counts=?, log=?, report_path=? WHERE id=?",
                         (db.now(), "stopped" if stopped else "done", db.dumps({**counts, "sources": sources_report}),
                          "\n".join(_state["lines"]), str(report_path), run_id))
            if only is None and not extra_jobs:
                db.kv_set(conn, "last_refresh", db.now())
        _say(f"Done: {counts['new']} new, {counts['updated']} updated, {counts['expired']} expired")
        if housekeeping.compact(db.DB_PATH, cleaned["cache_bytes"]):
            _say("Database compacted")
        return counts
    except Exception as e:
        _say(f"Refresh failed: {e}")
        for a in _state.get("agents") or []:
            a.stop()
        log = "\n".join(_state["lines"]) + "\n" + traceback.format_exc()
        for attempt in range(3):  # the failure may itself be a busy database
            try:
                with db.session() as conn:
                    conn.execute("UPDATE run SET ended=?, status='failed', log=? WHERE id=?", (db.now(), log, run_id))
                break
            except sqlite3.OperationalError:
                time.sleep(5 * (attempt + 1))
        raise
    finally:
        llm.embed_options = {}
        client.close()
        with _lock:
            _state["running"] = False
            _state["stopping"] = False


def score_mod():
    from vs import score
    return score


def rescore(rerank: bool = True) -> dict:
    """Score again without fetching; shows progress like a refresh."""
    with _lock:
        if _state["running"]:
            raise RuntimeError("A refresh or scoring is already running")
        _state.update(running=True, kind="score", stopping=False, lines=[], started=db.now())
    try:
        _say("Scoring again with your current portfolio, examples and corrections")
        portfolio = portfolio_mod.load()
        with db.session() as conn:
            expiry.expire(conn)
            merge_near_duplicates(conn, llm.embed_model())
            from vs import rules as rules_mod
            changes = rules_mod.apply(conn)
            if changes["hidden"] or changes["restored"]:
                _say(f"Your rules: {len(changes['hidden'])} hidden, {len(changes['restored'])} restored")
            conn.commit()
            out = score_mod().score_all(conn, portfolio, rerank=rerank, progress=_say)
        _say(f"Done: {out.get('scored', 0)} scored, {out.get('reranked', 0)} explained")
        return out
    except Exception as e:
        _say(f"Scoring failed: {e}")
        raise
    finally:
        with _lock:
            _state["running"] = False
            _state["stopping"] = False


def refresh_in_background(**kwargs) -> bool:
    if _state["running"]:
        return False
    threading.Thread(target=lambda: _safe(refresh, **kwargs), daemon=True).start()
    return True


_checks: dict[str, dict] = {}
_check_lock = threading.Lock()


def check_sites(sites: list[dict]) -> str:
    """Read a few named sites now, because a question in the chat asked about
    them: sources by id, a studio's jobs page once. Waits for a refresh that
    is already running. Returns a job id for check_state()."""
    job_id = uuid.uuid4().hex[:12]
    job = _checks[job_id] = {"state": "waiting" if _state["running"] else "running", "sites": sites, "error": ""}
    only = [s["id"] for s in sites if s["kind"] == "source"]
    extra = [(s["id"], {"adapter": "watch", "name": s["name"], "url": s["url"]}) for s in sites if s["kind"] == "studio"]

    def run():
        with _check_lock:  # one check at a time, in the order asked
            while True:
                while _state["running"]:
                    job["state"] = "waiting"
                    time.sleep(2)
                job["state"] = "running"
                try:
                    refresh(force=True, only=only, extra_jobs=extra, rerank=False)
                    job["state"] = "done"
                    return
                except RuntimeError as e:
                    if "already running" not in str(e):  # someone started a refresh first: wait again
                        job.update(state="failed", error=str(e))
                        return
                except Exception as e:
                    job.update(state="failed", error=f"{type(e).__name__}: {e}")
                    return
    threading.Thread(target=run, daemon=True).start()
    return job_id


def check_state(job_id: str) -> dict | None:
    return _checks.get(job_id)


def _safe(fn, **kwargs):
    try:
        fn(**kwargs)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- examples

def add_example(label: str, *, url: str = "", text: str = "", why: str = "", title: str = "") -> dict:
    """Save first (never lose it), then extract, then feed it if still open."""
    fetched, err = "", "empty page"
    if url and not text.strip():
        client = PoliteClient()
        try:
            fetched = page_text(client.get(url).text, url)
        except Exception as e:
            err = str(e)
        finally:
            client.close()
        if not fetched:
            ex = save_example(label, f"(Could not fetch {url}; paste the text to improve matching.)", url=url, why=why, title=title)
            return {"example": ex.id, "status": "saved_without_text", "error": err}
    body = text.strip() or fetched
    ex = save_example(label, body, url=url, why=why, title=title)
    try:
        lang = detect_lang(body)
        data = extract.extract(body, title=title, url=url, lang=lang)
        checks = extract.verify(data, body, lang)
    except llm.LLMError as e:
        return {"example": ex.id, "status": "saved_not_extracted", "error": str(e)}
    ex.extracted = {k: data.get(k) for k in ("title_en", "org_name", "type", "field_tag", "summary_en", "deadline",
                                             "location_country", "remote_mode")}
    write_example(ex)
    result = {"example": ex.id, "status": "saved"}
    deadline = data.get("deadline") or ""
    if data.get("is_opportunity") and expiry.status_for({**data, "deadline": deadline}) == "open":
        item = RawItem(url=url or f"example://{ex.id}", title=title or data.get("title_en") or ex.id,
                       source_id="example", text=body, needs_detail=False)
        row = build_row(item, data, checks, body, lang)
        with db.session() as conn:
            key = canonical_url(item.url) if url else item.url
            upsert(conn, key, item, row, content_hash(body), from_example=ex.id)
        result["in_feed"] = True
    return result
