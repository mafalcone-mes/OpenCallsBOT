"""Change monitoring for studio / institution pages on the watchlist: main
text hash + new links. New links that look like calls become RawItems."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin

import trafilatura
from selectolax.parser import HTMLParser

from vs import db
from vs.fetch import RawItem
from vs.fetch.polite import PoliteClient, canonical_url, domain

CALLISH = re.compile(
    r"call|job|vacanc|vacature|career|residen|fellow|grant|open-?call|position|intern|stage|bando|lavora|"
    r"opportunit|apply|beurs|stelle|convocatoria|beca|premio|prize|award|phd|postdoc", re.I)


def check(client: PoliteClient, entry: dict) -> tuple[bool, list[RawItem]]:
    """Returns (changed, candidate items)."""
    url = entry["url"]
    r = client.get(url)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    text = trafilatura.extract(r.text, include_links=False) or HTMLParser(r.text).text(separator=" ")
    norm = re.sub(r"\s+", " ", text or "").strip()
    h = hashlib.sha256(norm.encode()).hexdigest()
    tree = HTMLParser(r.text)
    host = domain(url)
    links: dict[str, str] = {}
    for a in tree.css("a[href]"):
        href = urljoin(str(r.url), a.attributes.get("href") or "")
        label = a.text(strip=True)
        if href.startswith("http") and domain(href) == host and (CALLISH.search(href) or CALLISH.search(label)):
            links[canonical_url(href)] = label
    links.pop(canonical_url(url), None)

    with db.session() as conn:
        prev = conn.execute("SELECT * FROM page_snapshot WHERE url=?", (url,)).fetchone()
        old_links = set(json.loads(prev["links"])) if prev else set()
        changed = prev is None or prev["text_hash"] != h
        conn.execute(
            "INSERT INTO page_snapshot(url, text_hash, text, links, checked_at, changed_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET text_hash=excluded.text_hash, text=excluded.text, links=excluded.links, "
            "checked_at=excluded.checked_at, changed_at=CASE WHEN page_snapshot.text_hash=excluded.text_hash "
            "THEN page_snapshot.changed_at ELSE excluded.changed_at END",
            (url, h, norm[:20000], json.dumps(sorted(links)), db.now(), db.now()),
        )
    new = [u for u in links if u not in old_links]
    items = [RawItem(url=u, title=links[u] or entry["name"], source_id=f"watch:{entry['name']}",
                     snippet=f"{entry['name']}: {links[u]}", hints={"org_name": entry["name"]}) for u in new[:10]]
    # The page itself may be the call (e.g. a single "opportunities" page).
    if changed and not items and len(norm) > 300:
        items.append(RawItem(url=url, title=f"{entry['name']}: opportunities page changed", source_id=f"watch:{entry['name']}",
                             snippet=norm[:800], text=norm[:12000], hints={"org_name": entry["name"]}, needs_detail=False))
    return changed, items
