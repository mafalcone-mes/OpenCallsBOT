"""'What the app thinks I like': deterministic stats + an LLM summary of the
examples and signals, and source suggestions from liked examples."""

from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

from vs import db, llm
from vs.config import prefs, update_prefs
from vs.examples import examples_hash, list_examples, taste_signals

STOP = set("""a an the and or of to in for on with at by from as is are be this that it its your you we our their
will can may not all any more most other such into about over under per via also who what which how their they
il lo la i gli le un una di da in con su per tra fra e o che del della dei degli delle al alla ai agli alle
de het een en van voor met op te in is die dat der die das und oder mit für von zu el la los las y o de del con
para por que""".split())


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zà-ÿ][a-zà-ÿ-]{2,}", (text or "").lower()) if w not in STOP]


def summary() -> dict:
    """Readable stats of positives vs negatives (c-TF-IDF-like distinctive terms)."""
    with db.session() as conn:
        opp = {r["dedup_key"]: dict(r) for r in conn.execute(
            "SELECT dedup_key, title_en, summary_en, field_tag, type, org_name, location_country, conditions FROM opportunity")}
    pos_docs, neg_docs = [], []
    types, orgs, countries, reds = Counter(), Counter(), Counter(), []
    for s in taste_signals():
        if s["ref"].startswith("example:"):
            ex = next((e for e in list_examples() if f"example:{e.id}" == s["ref"]), None)
            if not ex:
                continue
            text = " ".join(filter(None, [ex.extracted.get("title_en"), ex.extracted.get("summary_en"),
                                          ex.extracted.get("field_tag"), ex.text[:2000]]))
            meta = ex.extracted
        else:
            o = opp.get(s["ref"][4:])
            if not o:
                continue
            text = " ".join(filter(None, [o["title_en"], o["summary_en"], o["field_tag"]]))
            meta = o
        (pos_docs if s["w"] > 0 else neg_docs).append(text)
        if s["w"] > 0:
            if meta.get("type"):
                types[meta["type"]] += 1
            if meta.get("org_name"):
                orgs[meta["org_name"]] += 1
            if meta.get("location_country"):
                countries[meta["location_country"]] += 1
        elif s.get("why"):
            reds.append(s["why"])
    pos_tf = Counter(t for d in pos_docs for t in set(_tokens(d)))
    neg_tf = Counter(t for d in neg_docs for t in set(_tokens(d)))
    n_pos, n_neg = max(1, len(pos_docs)), max(1, len(neg_docs))

    def distinct(a: Counter, na: int, b: Counter, nb: int) -> list[str]:
        scored = {t: (c / na) * math.log(1 + (c / na) / ((b.get(t, 0) + 0.5) / nb)) for t, c in a.items() if c >= 2 or na <= 3}
        return [t for t, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:15]]

    return {"n_pos": len(pos_docs), "n_neg": len(neg_docs),
            "themes": distinct(pos_tf, n_pos, neg_tf, n_neg) if pos_docs else [],
            "anti_themes": distinct(neg_tf, n_neg, pos_tf, n_pos) if neg_docs else [],
            "types": types.most_common(6), "orgs": orgs.most_common(8), "countries": countries.most_common(6),
            "red_flags": reds[:12]}


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraph": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "types": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "organisations": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "red_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["paragraph", "themes", "types", "organisations", "red_flags"],
}


def llm_summary() -> dict:
    exs = list_examples()
    lines = []
    for ex in exs[-40:]:
        label = "LIKED" if ex.label == "interested" else "NOT INTERESTED"
        what = ex.extracted.get("title_en") or ex.url or ex.text[:80]
        lines.append(f"- {label}: {what} ({ex.extracted.get('field_tag', '')}; {ex.extracted.get('type', '')})"
                     f"{' — why: ' + ex.why if ex.why else ''}")
    stats = summary()
    user = (f"Examples he gave:\n{chr(10).join(lines) or '(none)'}\n\nDistinctive terms in liked items: {', '.join(stats['themes'])}\n"
            f"Terms in disliked items: {', '.join(stats['anti_themes'])}\nPreferences file reference studios: "
            f"{', '.join(prefs().get('taste', {}).get('reference_studios', []))}")
    out = llm.chat_json(
        "Summarise this person's taste in opportunities from their examples, in plain second person ('You seem to like...'). "
        "Be specific and honest about uncertainty when there are few examples. Red flags = what they reject.",
        user, SUMMARY_SCHEMA, temperature=0.2)
    out["examples_hash"] = examples_hash()
    with db.session() as conn:
        db.kv_set(conn, "taste_summary", out)
    return out


# ---------------------------------------------------------------- sources

CAREER_PATHS = ["careers", "jobs", "vacancies", "vacatures", "lavora-con-noi", "open-call", "open-calls",
                "opportunities", "residency", "join-us", "work-with-us", "about/opportunities"]


def suggest_sources() -> list[dict]:
    """For liked examples, propose the organisation's career / open-call page
    (and RSS feed) to watch. Nothing is added until confirmed."""
    import httpx
    from selectolax.parser import HTMLParser

    from vs.config import user_agent
    from vs.fetch.polite import domain

    p = prefs()
    watched = {domain(w["url"]) for w in p.get("watchlist", [])}
    watched |= {domain(s.get("url", "")) for s in p.get("sources", {}).values() if s.get("url")}
    with db.session() as conn:
        current = db.kv_get(conn, "source_suggestions") or []
        rejected = set(db.kv_get(conn, "source_rejected") or [])
    known = {s["url"] for s in current} | rejected
    new = []
    client = httpx.Client(headers={"User-Agent": user_agent()}, follow_redirects=True, timeout=15)
    try:
        for ex in list_examples():
            if ex.label != "interested" or not ex.url.startswith("http"):
                continue
            host = domain(ex.url)
            if host in watched or any(domain(s["url"]) == host for s in current + new):
                continue
            base = f"{urlparse(ex.url).scheme}://{urlparse(ex.url).netloc}/"
            found = None
            try:
                home = client.get(base)
                tree = HTMLParser(home.text)
                for a in tree.css("a[href]"):
                    href = urljoin(str(home.url), a.attributes.get("href") or "")
                    if domain(href) == host and any(f"/{c}" in href.lower() for c in CAREER_PATHS):
                        found = href
                        break
                feed = next((urljoin(str(home.url), n.attributes.get("href") or "") for n in tree.css("link[rel=alternate]")
                             if "rss" in (n.attributes.get("type") or "") or "atom" in (n.attributes.get("type") or "")), None)
            except httpx.HTTPError:
                continue
            for url in filter(None, [found]):
                if url not in known:
                    new.append({"url": url, "name": ex.extracted.get("org_name") or host, "from": ex.id,
                                "feed": feed, "kind": "watch"})
    finally:
        client.close()
    if new:
        with db.session() as conn:
            db.kv_set(conn, "source_suggestions", current + new)
    return new


def resolve_suggestion(url: str, name: str, accept: bool) -> None:
    with db.session() as conn:
        current = db.kv_get(conn, "source_suggestions") or []
        db.kv_set(conn, "source_suggestions", [s for s in current if s["url"] != url])
        if not accept:
            db.kv_set(conn, "source_rejected", sorted(set(db.kv_get(conn, "source_rejected") or []) | {url}))
    if accept:
        import tomlkit

        def mutate(doc):
            if "watchlist" not in doc:
                doc["watchlist"] = tomlkit.aot()
            entry = tomlkit.table()
            entry["name"] = name or urlparse(url).netloc
            entry["url"] = url
            entry["tags"] = ["suggested"]
            doc["watchlist"].append(entry)
        update_prefs(mutate)
