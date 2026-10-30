"""Scoring: portfolio fit + taste + keywords + source weight, then an LLM
rerank with explanations. Every component is stored so the UI can show it."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date

import numpy as np

from vs import db, embed, llm
from vs.config import prefs
from vs.examples import get_example, list_examples, taste_signals
from vs.portfolio import Portfolio


# ---------------------------------------------------------------- helpers

def _cos(m: np.ndarray, v: np.ndarray) -> np.ndarray:
    return m @ v if m.size else np.zeros(0)


def _kw_regex(kw: str) -> str:
    """Whole words (plural -s allowed); a trailing * marks a stem: fotograf* ->
    fotografo, fotografia, Fotograf. Avoids 'intern' matching 'international'."""
    if kw.endswith("*"):
        return rf"(?<!\w){re.escape(kw[:-1].lower())}"
    return rf"(?<!\w){re.escape(kw.lower())}(?:s|es)?(?!\w)"


def keyword_score(text: str, taste: dict) -> tuple[float, list[str]]:
    t = text.lower()
    hits, score = [], 0.0
    for kw in taste.get("keywords_plus", []) + taste.get("reference_studios", []):
        if re.search(_kw_regex(kw), t):
            hits.append(f"+{kw.rstrip('*')}")
            score += 0.25
    for kw in taste.get("keywords_minus", []):
        if re.search(_kw_regex(kw), t):
            hits.append(f"−{kw.rstrip('*')}")
            score -= 0.35
    return max(-1.0, min(1.0, score)), hits


# Fixed scales: cosine similarity at the first value counts 0, at the second 1.
# Set from real matches (projects: all ~0.43, starred ~0.46; taste: all ~0.25,
# starred ~0.43); they depend on the embedding model, not on the batch.
DEFAULT_RANGES = {"portfolio_range": (0.38, 0.52), "taste_range": (0.20, 0.50)}


def scaled(x: float, w: dict, key: str) -> float:
    lo, hi = w.get(key, DEFAULT_RANGES[key])
    return min(1.0, max(0.0, (x - lo) / ((hi - lo) or 1.0)))


def score_parts(w: dict, n_eff: float) -> dict:
    """Weights of the absolute score. Taste ramps up with the number of
    signals, so a new copy with no stars leans on the portfolio and the model."""
    ramp = min(1.0, n_eff / float(w.get("taste_full_at", 15))) if n_eff else 0.0
    return {"portfolio": float(w.get("score_portfolio", 0.4)), "taste": float(w.get("score_taste", 0.3)) * ramp,
            "model": float(w.get("score_model", 0.3)), "keyword_bonus": float(w.get("keyword_bonus", 5))}


def absolute_score(pn: float, tn: float, fit_n: float | None, k: float, parts: dict) -> float:
    """0-100: how close one opportunity is to you. Parts without a value
    (no model judgement yet) leave their weight to the others."""
    have = [(parts["portfolio"], pn), (parts["taste"], tn)] + ([(parts["model"], fit_n)] if fit_n is not None else [])
    total = sum(wt for wt, _ in have) or 1.0
    base = 100 * sum(wt * v for wt, v in have) / total
    return min(100.0, max(0.0, base + parts["keyword_bonus"] * min(1.0, max(0.0, k))))


def taste_hash(signals: list[dict], taste: dict) -> str:
    payload = json.dumps([[s["ref"], round(s["w"], 2), s.get("why", "")] for s in signals] + [taste.get("statements", [])],
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def eligibility_check(o: dict, me: dict) -> tuple[str, list[str]]:
    """ok | likely_ineligible | check. A badge only: nothing is hidden."""
    el = o.get("eligibility") or {}
    notes, bad, unsure = [], False, False
    # Limits the checker could not confirm on the page never mark you ineligible.
    confirmed = (o.get("field_confidence") or {}).get("eligibility", {}).get("verified", False)
    by = me.get("birth_year") or 0
    if by:
        age = date.today().year - by
        if el.get("age_max") and age > int(el["age_max"]):
            bad = True
            notes.append(f"age limit {el['age_max']} (you are ~{age})")
        if el.get("age_min") and age < int(el["age_min"]):
            bad = True
            notes.append(f"minimum age {el['age_min']}")
    elif el.get("age_max") or el.get("age_min"):
        unsure = True
        notes.append("age limit: set birth_year in preferences")
    mine = {c.upper() for c in me.get("citizenships", [])}
    eu = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
          "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
    nats = {n.upper() for n in el.get("nationalities") or []}
    if nats and mine:
        ok = bool(nats & mine) or ("EU" in nats and bool(mine & eu))
        if not ok:
            bad = True
            notes.append(f"nationality: {', '.join(sorted(nats))}")
    res = {r.upper() for r in el.get("residence") or []}
    home = (me.get("residence_country") or "").upper()
    if res:
        if not home:
            unsure = True
            notes.append(f"residence required: {', '.join(sorted(res))}")
        elif not (home in res or ("EU" in res and home in eu)):
            bad = True
            notes.append(f"residence: {', '.join(sorted(res))}")
    if (o.get("conditions") or {}).get("for_organisations"):
        unsure = True
        notes.append("for organisations: needs a partner organisation")
    langs = {k.lower() for k in (me.get("languages") or {})}
    app_langs = {l.lower() for l in o.get("application_languages") or []}
    if app_langs and langs and not (app_langs & langs):
        unsure = True
        notes.append(f"apply in: {', '.join(sorted(app_langs))}")
    if bad and not confirmed:
        bad, unsure = False, True
        notes = [f"{n} (unconfirmed)" for n in notes]
    return ("likely_ineligible" if bad else "check" if unsure else "ok"), notes


# ---------------------------------------------------------------- scoring

def context(conn, portfolio: Portfolio) -> dict:
    """Everything one score needs that doesn't depend on the opportunity:
    portfolio angles and taste signals. Built once per run."""
    p = prefs()
    w = p.get("weights", {})
    taste = p.get("taste", {})
    model = llm.embed_model()
    angles = embed.angle_vectors(conn, portfolio, model)
    a_keys, a_mat = embed.matrix(angles)

    # Taste vectors: examples by their text, implicit signals by the opportunity vector.
    signals = taste_signals()
    ex_texts = {}
    for ex in list_examples():
        ex_texts[f"example:{ex.id}"] = "\n".join(filter(None, [ex.extracted.get("title_en"), ex.extracted.get("summary_en"), ex.text[:3000]]))
    ex_vecs = embed.ensure(conn, "example", ex_texts, model) if ex_texts else {}
    # Your written corrections count as liked examples too (weight 1, no fading).
    statements = [s for s in taste.get("statements", []) if s.strip()]
    st_vecs = embed.ensure(conn, "statement", {f"statement:{i}": s for i, s in enumerate(statements)}, model) if statements else {}
    ex_vecs.update(st_vecs)
    signals = signals + [{"ref": f"statement:{i}", "w": 1.0, "why": s, "label": "correction", "title": s[:80]}
                         for i, s in enumerate(statements)]
    # Projects by studios you admire: together they weigh like `reference_total`
    # liked examples (no fading). Every studio gets an equal share, split across
    # its projects, so a studio with many projects doesn't outweigh the others.
    from vs import references
    refs = references.load()
    if refs:
        ref_vecs = embed.ensure(conn, "reference", {f"reference:{r['id']}": f"{r['title']}\n{r['text'][:3000]}" for r in refs}, model)
        ex_vecs.update(ref_vecs)
        per_studio = Counter(r["studio"] for r in refs)
        w_studio = float(w.get("reference_total", 3.0)) / len(per_studio)
        signals = signals + [{"ref": f"reference:{r['id']}", "w": w_studio / per_studio[r["studio"]], "why": "",
                              "label": "reference", "title": f"{r['title']} ({r['studio']})"} for r in refs]
    all_opp_vecs = db.all_vecs(conn, "opportunity", model)
    sig_vecs, sig_w, sig_meta = [], [], []
    opp_titles = {r["dedup_key"]: r["title_en"] or r["title_original"] for r in conn.execute(
        "SELECT dedup_key, title_en, title_original FROM opportunity WHERE dedup_key IN "
        "(SELECT opportunity_key FROM user_state)")}
    for s in signals:
        ref = s["ref"]
        v = ex_vecs.get(ref) if ref.startswith(("example:", "statement:", "reference:")) else all_opp_vecs.get(ref.split(":", 1)[1])
        if v is not None and abs(s["w"]) > 0.01:
            if ref.startswith("opp:") and not s.get("title"):
                s = {**s, "title": opp_titles.get(ref[4:], "")}
            sig_vecs.append(v)
            sig_w.append(s["w"])
            sig_meta.append(s)
    sig_w = np.array(sig_w)
    n_eff = float(np.abs(sig_w).sum()) if len(sig_w) else 0.0
    conn.commit()  # embeddings saved: other parts of the app can write
    return {"portfolio": portfolio, "p": p, "w": w, "taste": taste, "me": p.get("me", {}), "sources": p.get("sources", {}),
            "model": model, "a_keys": a_keys, "a_mat": a_mat,
            "sig_mat": np.vstack(sig_vecs) if sig_vecs else np.zeros((0, 1)), "sig_w": sig_w, "sig_meta": sig_meta,
            "n_eff": n_eff, "lam": float(w.get("negative_lambda", 0.7)), "t_hash": taste_hash(signals, taste),
            "parts": score_parts(w, n_eff)}


def prepare(ctx: dict, o: dict, v: np.ndarray) -> dict:
    """The parts of one opportunity's score that need no model call."""
    w, sig_w, sig_mat, sig_meta = ctx["w"], ctx["sig_w"], ctx["sig_mat"], ctx["sig_meta"]
    # Portfolio similarity: mean of the top-2 angles.
    a_sims = _cos(ctx["a_mat"], v)
    top = np.sort(a_sims)[::-1]
    P = float(top[:2].mean()) if top.size else 0.0
    T, similar, near_pos, near_neg = 0.0, [], [], []
    if len(sig_w):
        sims = _cos(sig_mat, v)
        pos, neg = sig_w > 0, sig_w < 0

        def _weighted(mask):
            if not mask.any():
                return 0.0, []
            idx = np.where(mask)[0]
            top = idx[np.argsort(sims[idx])[::-1][:5]]
            ww = np.abs(sig_w[top])
            return float((sims[top] * ww).sum() / ww.sum()), list(top)

        tp, top_pos = _weighted(pos)
        tn, top_neg = _weighted(neg)
        T = tp - ctx["lam"] * tn
        similar = [sig_meta[i]["ref"] for i in top_pos[:3]
                   if sims[i] > 0.55 and not sig_meta[i]["ref"].startswith("statement:")]
        near_pos = [sig_meta[i]["ref"] for i in top_pos[:3]]
        near_neg = [sig_meta[i]["ref"] for i in top_neg[:2]]
    text = " ".join(filter(None, [o.get("title_en"), o.get("title_original"), o.get("summary_en"), o.get("field_tag"),
                                  (o.get("raw_text") or "")[:4000]]))
    K, hits = keyword_score(text, ctx["taste"])
    S = float(ctx["sources"].get(o.get("source_id") or "", {}).get("weight", 0.6))
    # Europe first: a small bonus by region (remote counts as reachable).
    regions = w.get("region", {})
    region = "remote" if o.get("remote_mode") in ("remote", "online") else (o.get("region") or "unknown")
    R = float(regions.get(region, regions.get("unknown", 0.0)))
    # nearest angles for suggestions
    best = [ctx["a_keys"][i] for i in np.argsort(a_sims)[::-1][:3]] if a_sims.size else []
    flag, notes = eligibility_check(o, ctx["me"])
    # The score says how close it is to you, on fixed scales: it never depends
    # on what else was found. Source and region only break ties.
    Pn, Tn = scaled(P, w, "portfolio_range"), scaled(T, w, "taste_range")
    return {"o": o, "P": P, "T": T, "K": K, "S": S, "R": R, "Pn": Pn, "Tn": Tn, "hits": hits, "similar": similar,
            "near_pos": near_pos, "near_neg": near_neg, "best_angles": best, "flag": flag, "notes": notes,
            "prelim": absolute_score(Pn, Tn, None, K, ctx["parts"]),
            "tiebreak": float(w.get("source", 0.2)) * S + R}


def _saved_explanation(prev: dict) -> dict:
    return {"fit": prev["llm_fit"], "reasons": json.loads(prev["reasons"] or "[]"),
            "red_flags": json.loads(prev["red_flags"] or "[]"),
            "suggested_projects": json.loads(prev["suggested_projects"] or "[]"),
            "similar_to": (json.loads(prev["similar_examples"] or "[]") or [""])[0],
            "cluster": prev.get("cluster") or ""}


def write_match(conn, ctx: dict, r: dict, llm_out: dict | None, key: str, bucket: str, discovery_reason: str = "") -> float:
    """Saves one score; returns it (0-1)."""
    o = r["o"]
    fit_n = (float(llm_out["fit"]) - 1) / 4 if llm_out else None  # not explained: the other parts stand alone
    final = absolute_score(r["Pn"], r["Tn"], fit_n, r["K"], ctx["parts"]) / 100
    reasons = (llm_out or {}).get("reasons") or ([f"Keywords: {', '.join(r['hits'][:4])}"] if r["hits"] else [])
    similar = r["similar"]
    if llm_out and llm_out.get("similar_to") and llm_out["similar_to"] in [s["ref"] for s in ctx["sig_meta"]]:
        similar = [llm_out["similar_to"]] + [s for s in similar if s != llm_out["similar_to"]]
    suggested = (llm_out or {}).get("suggested_projects") or [{"angle": k, "why": ""} for k in r["best_angles"][:2]]
    conn.execute(
        """INSERT INTO match(opportunity_id, computed_at, profile_hash, taste_hash, portfolio_sim, taste, keyword, source_w,
             prelim, llm_fit, final, bucket, discovery_reason, cluster, reasons, similar_examples, red_flags,
             suggested_projects, eligibility_flag, eligibility_notes, rerank_key, tiebreak)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(opportunity_id) DO UPDATE SET computed_at=excluded.computed_at, profile_hash=excluded.profile_hash,
             taste_hash=excluded.taste_hash, portfolio_sim=excluded.portfolio_sim, taste=excluded.taste,
             keyword=excluded.keyword, source_w=excluded.source_w, prelim=excluded.prelim, llm_fit=excluded.llm_fit,
             final=excluded.final, bucket=excluded.bucket, discovery_reason=excluded.discovery_reason,
             cluster=excluded.cluster, reasons=excluded.reasons, similar_examples=excluded.similar_examples,
             red_flags=excluded.red_flags, suggested_projects=excluded.suggested_projects,
             eligibility_flag=excluded.eligibility_flag, eligibility_notes=excluded.eligibility_notes,
             rerank_key=excluded.rerank_key, tiebreak=excluded.tiebreak""",
        (o["id"], db.now(), ctx["portfolio"].profile_hash, ctx["t_hash"], r["Pn"], r["Tn"], r["K"], r["S"], r["prelim"] / 100,
         (llm_out or {}).get("fit"), final, bucket, discovery_reason, (llm_out or {}).get("cluster", ""),
         db.dumps(reasons), db.dumps(similar), db.dumps((llm_out or {}).get("red_flags", [])),
         db.dumps(suggested), r["flag"], db.dumps(r["notes"]), key if llm_out else None, r["tiebreak"]),
    )
    return final


def score_one(conn, ctx: dict, opp_id: int, *, rerank: bool = True, progress=lambda msg: None) -> float | None:
    """Score one opportunity as soon as it is read, so it shows up in the feed
    right away. The model explains it when its score without the model already
    reaches `explain_from` (an absolute line too: no need to wait for the others)."""
    o = db.row_dict(conn.execute("SELECT * FROM opportunity WHERE id=? AND status='open' AND extraction_status='done'",
                                 (opp_id,)).fetchone())
    if not o:
        return None
    v = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o)}, ctx["model"])[o["dedup_key"]]
    conn.commit()  # never hold the database while the model thinks
    r = prepare(ctx, o, v)
    prev = db.row_dict(conn.execute("SELECT * FROM match WHERE opportunity_id=?", (opp_id,)).fetchone())
    user, schema = rerank_prompt(ctx["portfolio"], o, r, ctx["sig_meta"], ctx["taste"])
    key = rerank_key(user, schema)
    llm_out = None
    if prev and prev.get("llm_fit") is not None and prev.get("rerank_key") == key:
        llm_out = _saved_explanation(prev)
    elif rerank and r["prelim"] >= float(ctx["w"].get("explain_from", 40)):
        progress(f"Explaining: {(o.get('title_en') or '')[:60]}")
        try:
            llm_out = rerank_one(ctx["portfolio"], user, schema, ctx["sig_meta"])
        except llm.LLMError as e:
            progress(f"Explanation skipped: {e}")
    final = write_match(conn, ctx, r, llm_out, key, (prev or {}).get("bucket") or "feed",
                        (prev or {}).get("discovery_reason") or "")
    conn.commit()
    return final


def score_all(conn, portfolio: Portfolio, *, rerank: bool = True, progress=lambda msg: None,
              stale_limit: int | None = None) -> dict:
    """`stale_limit`: at most this many explanations that only went out of date
    (e.g. after a new star) are redone; the others keep their old text until a
    later run. New matches are always explained. None = redo all of them."""
    opps = [db.row_dict(r) for r in conn.execute(
        "SELECT * FROM opportunity WHERE status='open' AND extraction_status='done'")]
    if not opps:
        return {"scored": 0}
    ctx = context(conn, portfolio)
    w, model, sig_w = ctx["w"], ctx["model"], ctx["sig_w"]
    o_vecs = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o) for o in opps}, model)
    rows = [prepare(ctx, o, o_vecs[o["dedup_key"]]) for o in opps]
    rows.sort(key=lambda r: (round(r["prelim"]), r["tiebreak"]), reverse=True)

    # Discovery candidates are chosen before rerank so they get explanations too.
    from vs.discovery import pick_discovery
    discovery = pick_discovery(conn, rows, o_vecs, ctx["sig_mat"][sig_w > 0] if len(sig_w) else np.zeros((0, 1)), ctx["p"])
    # The model explains the best ones, but never those already far from you.
    top_k, explain_from = int(w.get("rerank_top_k", 40)), float(w.get("explain_from", 40))
    to_rerank = {r["o"]["dedup_key"] for r in rows[:top_k] if r["prelim"] >= explain_from} | set(discovery)

    conn.commit()  # embeddings saved before the slow rerank
    llm.unload(model)  # embeddings done: free memory before the chat model loads
    existing = {m["opportunity_id"]: dict(m) for m in conn.execute("SELECT * FROM match")}
    reranked, stale_left = 0, stale_limit
    for r in rows:
        o = r["o"]
        prev = existing.get(o["id"])
        user, schema = rerank_prompt(portfolio, o, r, ctx["sig_meta"], ctx["taste"])
        key = rerank_key(user, schema)
        llm_out = None
        explained = bool(prev) and prev.get("llm_fit") is not None
        stale = explained and prev.get("rerank_key") != key and rerank and o["dedup_key"] in to_rerank
        if stale and stale_left is not None:
            if stale_left > 0:
                stale_left -= 1
            else:
                key = prev["rerank_key"]  # keep the old text; redone at a later run
        if explained and prev.get("rerank_key") == key:
            llm_out = _saved_explanation(prev)
        elif rerank and o["dedup_key"] in to_rerank:
            progress(f"Explaining match {reranked + 1}/{len(to_rerank)}: {(o.get('title_en') or '')[:60]}")
            try:
                llm_out = rerank_one(portfolio, user, schema, ctx["sig_meta"])
                reranked += 1
            except llm.LLMError as e:
                progress(f"Rerank skipped: {e}")
        bucket = "discovery" if o["dedup_key"] in discovery else "feed"
        write_match(conn, ctx, r, llm_out, key, bucket, discovery.get(o["dedup_key"], ""))
        conn.commit()  # every row: never hold the database while the model thinks
    db.kv_set(conn, "last_scored", {"at": db.now(), "profile_hash": portfolio.profile_hash, "taste_hash": ctx["t_hash"],
                                    "n_eff": ctx["n_eff"], "w_taste": ctx["parts"]["taste"]})
    return {"scored": len(rows), "reranked": reranked, "discovery": len(discovery), "n_eff": round(ctx["n_eff"], 2)}


# ---------------------------------------------------------------- rerank

RERANK_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "red_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "similar_to": {"type": "string"},
        "cluster": {"type": "string"},
    },
    "required": ["fit", "reasons", "red_flags", "similar_to", "cluster", "suggested_projects"],
}

RERANK_VERSION = "4"  # bump when the prompt changes, so saved explanations are redone

def rerank_system(me: dict | None = None) -> str:
    """Instructions for the model, built from the person's [me] settings
    (seniority, formats they are open to) rather than written for one person."""
    me = me or {}
    seniority = me.get("seniority") or ""
    open_to = ", ".join(me.get("open_to") or [])
    level = (f"Seniority matters: they are {seniority}"
             + (f"; {open_to} are welcome" if open_to else "")
             + ("; roles clearly above that level are not (for now)." if seniority else ".")) if seniority else \
        "Take the seniority asked by the role into account."
    return f"""You help a person (described in PROFILE) decide which opportunities fit them.
Judge FIRST whether the role, field and tasks match what they are looking for: the opportunities they starred,
their keywords and their own corrections (the kinds of work they name). Their projects are only context: use them to
understand their interests and to suggest what to present, not as the main criterion. {level} Be concrete and brief.
- fit: 1 (irrelevant) to 5 (excellent, apply). Money conditions, fees and location never lower fit: they are shown separately.
- reasons: up to 3 short sentences. Start with the role/field match (which of their keywords or fields it hits),
  then, if useful, which project or reference work it relates to. Do not state facts about them that are not given.
- similar_to: the id of the ONE listed example/signal it most resembles, or "" if none is close.
- red_flags: mismatches or practical blockers stated in the text (e.g. "requires 5+ years", "senior role", "unpaid, no costs covered").
- cluster: 2 to 4 words naming the kind of opportunity (e.g. "civic tech fellowship").
- suggested_projects: 1 to 3 of their project angles to present, with the angle to stress."""


def rerank_prompt(portfolio: Portfolio, o: dict, r: dict, sig_meta: list[dict], taste: dict) -> tuple[str, dict]:
    """The exact prompt and schema the model sees for one opportunity. Their
    hash is the cache key of its explanation, so it is redone only when
    something it actually uses changes: the opportunity, their keywords and
    corrections, their projects, or its nearest examples and references. Not
    when an unrelated favourite is added or signal weights fade by a day."""
    angle_keys = [a.key for a in portfolio.angles]
    schema = json.loads(json.dumps(RERANK_SCHEMA_BASE))
    schema["properties"]["suggested_projects"] = {
        "type": "array", "maxItems": 3,
        "items": {"type": "object", "properties": {"angle": {"type": "string", "enum": angle_keys},
                                                   "why": {"type": "string"}}, "required": ["angle", "why"]}}
    relevant = []
    for key in r["best_angles"]:
        a = portfolio.angle(key)
        if a:
            relevant.append(f"[{a.key}] {a.project_name} as {portfolio.category(a.category).label}: {a.subtitle}. {a.body[:500]}")
    # nearest positive and negative signals with their "why" notes
    by_ref = {s["ref"]: s for s in sig_meta}
    refs = []
    for ref in r["near_pos"] + r["near_neg"]:
        s = by_ref[ref]
        label = ("THEIR OWN CORRECTION" if ref.startswith("statement:") else "WORK THEY ADMIRE" if ref.startswith("reference:")
                 else "LIKED" if s["w"] > 0 else "DISLIKED")
        refs.append(f"- id={s['ref']} ({label}{', why: ' + s['why'] if s.get('why') else ''}): {s.get('title', '')}")
    statements = taste.get("statements") or []
    starred = [s.get("title", "") for s in sig_meta if s.get("label") == "favourite" and s.get("title")][:15]
    user = f"""PROFILE
{portfolio.person.get('name')}: {portfolio.person.get('role')}. {' '.join(portfolio.person.get('bio') or [])}
Reference studios they admire: {', '.join(taste.get('reference_studios', []))}
{('Their own corrections about their taste: ' + '; '.join(statements)) if statements else ''}
{('OPPORTUNITIES THEY STARRED (the best evidence of what they want): ' + '; '.join(starred)) if starred else ''}
KEYWORDS THEY SEARCH FOR (roles, fields, techniques): {', '.join(taste.get('keywords_plus', [])[:40])}
KEYWORDS THAT PUT THEM OFF: {', '.join(taste.get('keywords_minus', []))}

THEIR PROJECTS, FOR CONTEXT (id in brackets; all ids: {', '.join(angle_keys)})
{chr(10).join(relevant)}

EXAMPLES AND SIGNALS FROM THEM
{chr(10).join(refs[:6]) if refs else '(none yet)'}

OPPORTUNITY
Title: {o.get('title_en') or o.get('title_original')}
Organisation: {o.get('org_name')}
Type: {o.get('type')} | Field: {o.get('field_tag')} | Location: {o.get('location_city')} {o.get('location_country')} {o.get('remote_mode')}
Summary: {o.get('summary_en')}
Text excerpt: {(o.get('raw_text') or '')[:2500]}"""
    return user, schema


def rerank_key(user: str, schema: dict) -> str:
    payload = f"{RERANK_VERSION}|{rerank_system(prefs().get('me', {}))}|{json.dumps(schema, sort_keys=True)}|{user}"
    return hashlib.sha256(payload.encode()).hexdigest()


def rerank_one(portfolio: Portfolio, user: str, schema: dict, sig_meta: list[dict]) -> dict:
    angle_keys = [a.key for a in portfolio.angles]
    out = llm.chat_json(rerank_system(prefs().get("me", {})), user, schema, temperature=0.1,
                        max_tokens=800, purpose="explain match")
    out["fit"] = max(1, min(5, int(out.get("fit") or 1)))
    valid_refs = {s["ref"] for s in sig_meta}
    if out.get("similar_to") not in valid_refs:
        out["similar_to"] = ""
    out["suggested_projects"] = [s for s in out.get("suggested_projects", []) if s.get("angle") in angle_keys]
    return out


def example_title(ref: str) -> str:
    if ref.startswith("reference:"):
        from vs import references
        r = next((x for x in references.load() if x["id"] == ref.split(":", 1)[1]), None)
        return f"{r['title']} by {r['studio']}" if r else ref
    if ref.startswith("example:"):
        ex = get_example(ref.split(":", 1)[1])
        if ex:
            return ex.extracted.get("title_en") or ex.url or ex.id
    return ref
