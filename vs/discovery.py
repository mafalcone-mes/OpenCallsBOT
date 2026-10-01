"""The Discovery area: less obvious matches worth exploring, chosen from
outside the top of the feed and kept diverse with MMR."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np


def _adjacent_hit(o: dict, fields: list[str]) -> str:
    text = " ".join(filter(None, [o.get("field_tag"), o.get("title_en"), o.get("summary_en")])).lower()
    for f in fields:
        words = [w for w in f.lower().split() if len(w) > 3]
        if words and all(w in text for w in words):
            return f
    return ""


def pick_discovery(conn, rows: list[dict], o_vecs: dict, pos_mat: np.ndarray, p: dict) -> dict[str, str]:
    """rows are sorted by prelim (best first). Returns dedup_key -> reason."""
    n = len(rows)
    if n < 10:
        return {}
    share = float(p.get("weights", {}).get("discovery_share", 0.2))
    feed_top = max(10, int(n * 0.15))
    want = int(min(15, max(8, round(feed_top * share / (1 - share) * 2))))
    adjacent = p.get("taste", {}).get("adjacent_fields", [])
    recent = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    org_counts = {r["org_name"]: r["c"] for r in conn.execute(
        "SELECT org_name, COUNT(*) c FROM opportunity GROUP BY org_name")}
    dismissed = {r["opportunity_key"] for r in conn.execute("SELECT opportunity_key FROM user_state WHERE dismissed=1")}

    candidates: dict[str, str] = {}
    for rank, r in enumerate(rows):
        if rank < feed_top:
            continue
        o = r["o"]
        key = o["dedup_key"]
        if key in dismissed or o.get("type") == "other":
            continue
        pct = 1 - rank / n
        v = o_vecs[key]
        novelty = 1 - float((pos_mat @ v).max()) if pos_mat.size and pos_mat.shape[1] == v.shape[0] else 1.0
        adj = _adjacent_hit(o, adjacent)
        if adj:
            candidates[key] = f"Adjacent field: {adj}"
        elif 0.40 <= pct <= 0.75 and novelty > 0.45:
            candidates[key] = "Medium match, unlike anything you've marked so far"
        elif org_counts.get(o.get("org_name"), 0) == 1 and (o.get("first_seen") or "") >= recent and pct >= 0.3:
            candidates[key] = "New organisation for you"
    if not candidates:
        return {}

    # MMR: relevance = prelim rank, diversity = distance to already picked.
    rel = {r["o"]["dedup_key"]: 1 - i / n for i, r in enumerate(rows)}
    picked: list[str] = []
    pool = list(candidates)
    while pool and len(picked) < want:
        def mmr(k):
            if not picked:
                return rel[k]
            sim = max(float(o_vecs[k] @ o_vecs[j]) for j in picked)
            return 0.5 * rel[k] - 0.5 * sim
        best = max(pool, key=mmr)
        picked.append(best)
        pool.remove(best)
    return {k: candidates[k] for k in picked}
