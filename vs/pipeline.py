"""Refresh: fetch every source, extract new items with the LLM, dedupe,
expire, score. Also the flow for adding an example."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import traceback
from datetime import date, datetime, timedelta, timezone

import numpy as np
import trafilatura
from rapidfuzz import fuzz

from vs import db, embed, extract, llm
from vs import portfolio as portfolio_mod
from vs.config import prefs
from vs.examples import save_example, write_example
from vs.fetch import RawItem
from vs.fetch.adapters import ADAPTERS
from vs.fetch.polite import Blocked, PoliteClient, canonical_url, domain
from vs.fetch.watch import check as watch_check

PREFILTER_ADAPTERS = {"jobspy", "euraxess", "eu_ft"}

# ---------------------------------------------------------------- progress

_state = {"running": False, "run_id": None, "lines": [], "started": None}
_lock = threading.Lock()


def progress_state() -> dict:
    with _lock:
        return {**_state, "lines": list(_state["lines"][-12:])}


def _say(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _state["lines"].append(f"{stamp}  {msg}")
    print(f"{stamp}  {msg}", flush=True)


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
    today = date.today().isoformat()
    status = "open"
    if deadline and deadline[:10] < today:
        status = "expired"
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

def process_item(client: PoliteClient, conn, item: RawItem, model: str) -> str:
    """Returns: new | updated | unchanged | skipped | duplicate | failed"""
    key = canonical_url(item.url)
    existing = conn.execute("SELECT id, content_hash, extraction_status, source_urls, status, extract_version FROM opportunity WHERE dedup_key=?",
                            (key,)).fetchone()
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
    lang = detect_lang(text)
    data = extract.extract(text, title=item.title, url=item.url, lang=lang)
    if not data.get("is_opportunity", True):
        conn.execute(
            "INSERT INTO opportunity(dedup_key, canonical_url, source_id, title_original, content_hash, extraction_status, status, first_seen, last_seen, extract_version) "
            "VALUES(?,?,?,?,?,'done','not_opportunity',?,?,?) ON CONFLICT(dedup_key) DO UPDATE SET content_hash=excluded.content_hash, "
            "status='not_opportunity', last_seen=excluded.last_seen, extract_version=excluded.extract_version",
            (key, key, item.source_id, item.title, chash, db.now(), db.now(), extract.EXTRACT_VERSION))
        return "skipped"
    checks = extract.verify(data, text, lang)
    row = build_row(item, data, checks, text, lang)
    # Embeddings are computed later in one batch (merge_near_duplicates), so
    # the chat and embedding models don't swap in video memory per item.
    dup = find_duplicate(conn, row, None, model, key)
    if dup:
        _merge_into(conn, dup, [item.url])
        return "duplicate"
    upsert(conn, key, item, row, chash)
    return "updated" if existing else "new"


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
    today = date.today().isoformat()

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
        status = "expired" if d.get("deadline") and d["deadline"] < today else "open"
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
    """For high-volume sources: keep the `keep` items whose title+snippet is
    closest to the portfolio angles or liked examples. Skips known URLs."""
    fresh = []
    for it in items:
        row = conn.execute("SELECT extraction_status FROM opportunity WHERE dedup_key=?", (canonical_url(it.url),)).fetchone()
        if not row or row["extraction_status"] != "done":
            fresh.append(it)
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


# ---------------------------------------------------------------- refresh

def refresh(*, force: bool = False, only: str | None = None, rerank: bool = True) -> dict:
    with _lock:
        if _state["running"]:
            raise RuntimeError("A refresh is already running")
        _state.update(running=True, lines=[], started=db.now())
    counts = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "too_short": 0, "duplicate": 0, "failed": 0, "expired": 0}
    sources_report: dict[str, dict] = {}
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
        p = prefs()
        fetch_cfg = p.get("fetch", {})
        min_interval = timedelta(hours=float(fetch_cfg.get("source_min_interval_h", 12)))
        keep = int(fetch_cfg.get("detail_pages_per_source", 40))

        jobs: list[tuple[str, dict]] = [(sid, cfg) for sid, cfg in p.get("sources", {}).items() if cfg.get("enabled", True)]
        jobs += [(f"watch:{w['name']}", {"adapter": "watch", **w}) for w in p.get("watchlist", [])]
        if only:
            jobs = [j for j in jobs if j[0] == only]

        with db.session() as conn:
            # Phase 1: list every source (embedding model only, for the prefilter).
            queue: list[tuple[str, list[RawItem]]] = []
            for sid, cfg in jobs:
                st = conn.execute("SELECT * FROM source_state WHERE source_id=?", (sid,)).fetchone()
                if st and st["last_ok"] and not force:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_ok"])
                    if age < min_interval:
                        _say(f"{sid}: fetched {int(age.total_seconds() // 3600)} h ago, skipped (use force)")
                        continue
                if st and (st["blocked_runs"] or 0) >= 2 and cfg.get("adapter") == "jobspy" and not force:
                    _say(f"{sid}: auto-disabled after 2 blocked runs")
                    continue
                _say(f"{sid}: listing")
                rep = sources_report[sid] = {"items": 0, "processed": {}, "error": None}
                try:
                    if cfg["adapter"] == "watch":
                        changed, items = watch_check(client, cfg)
                        rep["changed"] = changed
                    else:
                        items = list(ADAPTERS[cfg["adapter"]](client, sid, cfg))
                    rep["items"] = len(items)
                    from vs import rules as rules_mod
                    active = rules_mod.compile_rules(p)
                    kept = [it for it in items if not rules_mod.reason(it.title, "", sid, active)]
                    if len(kept) < len(items):
                        _say(f"{sid}: {len(items) - len(kept)} skipped by your rules (title not in your fields / never-show)")
                    items = kept
                    if cfg.get("adapter") in PREFILTER_ADAPTERS or cfg.get("prefilter"):
                        before = len(items)
                        items = prefilter(conn, portfolio, items, int(cfg.get("keep", keep)), model)
                        _say(f"{sid}: {before} listed, {len(items)} new and closest to your work kept")
                    queue.append((sid, items))
                    rep["history"] = ((db.row_dict(st)["items_history"] or []) if st else [])[-9:] + [rep["items"]]
                except Exception as e:
                    blocked = isinstance(e, Blocked) or "429" in str(e)
                    rep["error"] = f"{type(e).__name__}: {e}"
                    _say(f"{sid}: ERROR {rep['error'][:200]}")
                    conn.execute(
                        "INSERT INTO source_state(source_id, last_fetch, error, blocked_runs) VALUES(?,?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET last_fetch=excluded.last_fetch, error=excluded.error, "
                        "blocked_runs=source_state.blocked_runs + ?",
                        (sid, db.now(), rep["error"][:500], int(blocked), int(blocked)))
                conn.commit()

            # Phase 2: read each new page with the chat model. Only one model is
            # kept in memory at a time (16 GB RAM).
            llm.unload(model)
            total = sum(len(items) for _, items in queue)
            done = 0
            for sid, items in queue:
                rep = sources_report[sid]
                for it in items:
                    done += 1
                    try:
                        res = process_item(client, conn, it, model)
                    except llm.LLMError as e:
                        _say(f"  LLM failed on {it.url}: {e}")
                        res = "failed"
                    counts[res] = counts.get(res, 0) + 1
                    rep["processed"][res] = rep["processed"].get(res, 0) + 1
                    if res != "unchanged":
                        label = {"skipped": "not a call", "too_short": "too little text on the page",
                                 "failed": "could not read"}.get(res, res)
                        _say(f"  [{done}/{total}] {label}: {it.title[:70]}")
                    conn.commit()
                # Only now is the source done: an interrupted run must not make
                # the next refresh skip it for the minimum interval.
                conn.execute(
                    "INSERT INTO source_state(source_id, last_fetch, last_ok, items_last, items_history, error, blocked_runs) "
                    "VALUES(?,?,?,?,?,NULL,0) ON CONFLICT(source_id) DO UPDATE SET last_fetch=excluded.last_fetch, "
                    "last_ok=excluded.last_ok, items_last=excluded.items_last, items_history=excluded.items_history, "
                    "error=NULL, blocked_runs=0",
                    (sid, db.now(), db.now(), rep["items"], db.dumps(rep.pop("history", []))))
                conn.commit()

            # Phase 3: embed in one batch, merge cross-listed calls, expire, score.
            _say("Comparing with your portfolio")
            llm.unload(llm.chat_model())
            counts["duplicate"] += merge_near_duplicates(conn, model)
            from vs import rules as rules_mod
            changes = rules_mod.apply(conn)
            if changes["hidden"] or changes["restored"]:
                _say(f"Your rules: {len(changes['hidden'])} hidden, {len(changes['restored'])} restored")
            conn.commit()
            today = date.today().isoformat()
            counts["expired"] = conn.execute(
                "UPDATE opportunity SET status='expired' WHERE status='open' AND deadline != '' AND deadline < ?", (today,)).rowcount
            conn.commit()
            _say("Scoring")
            score_counts = score_mod().score_all(conn, portfolio, rerank=rerank, progress=_say)
            counts.update({f"score_{k}": v for k, v in score_counts.items()})

        from vs.report import write_run_report
        report_path = write_run_report(run_id, counts, sources_report)
        with db.session() as conn:
            conn.execute("UPDATE run SET ended=?, status='done', counts=?, log=?, report_path=? WHERE id=?",
                         (db.now(), db.dumps({**counts, "sources": sources_report}), "\n".join(_state["lines"]),
                          str(report_path), run_id))
            db.kv_set(conn, "last_refresh", db.now())
        _say(f"Done: {counts['new']} new, {counts['updated']} updated, {counts['expired']} expired")
        return counts
    except Exception as e:
        _say(f"Refresh failed: {e}")
        with db.session() as conn:
            conn.execute("UPDATE run SET ended=?, status='failed', log=? WHERE id=?",
                         (db.now(), "\n".join(_state["lines"]) + "\n" + traceback.format_exc(), run_id))
        raise
    finally:
        client.close()
        with _lock:
            _state["running"] = False


def score_mod():
    from vs import score
    return score


def rescore(rerank: bool = True) -> dict:
    """Score again without fetching; shows progress like a refresh."""
    with _lock:
        if _state["running"]:
            raise RuntimeError("A refresh or scoring is already running")
        _state.update(running=True, lines=[], started=db.now())
    try:
        _say("Scoring again with your current portfolio, examples and corrections")
        portfolio = portfolio_mod.load()
        with db.session() as conn:
            conn.execute("UPDATE opportunity SET status='expired' WHERE status='open' AND deadline != '' AND deadline < date('now')")
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


def refresh_in_background(**kwargs) -> bool:
    if _state["running"]:
        return False
    threading.Thread(target=lambda: _safe(refresh, **kwargs), daemon=True).start()
    return True


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
    if data.get("is_opportunity") and (not deadline or deadline >= date.today().isoformat()):
        item = RawItem(url=url or f"example://{ex.id}", title=title or data.get("title_en") or ex.id,
                       source_id="example", text=body, needs_detail=False)
        row = build_row(item, data, checks, body, lang)
        with db.session() as conn:
            key = canonical_url(item.url) if url else item.url
            upsert(conn, key, item, row, content_hash(body), from_example=ex.id)
        result["in_feed"] = True
    return result
