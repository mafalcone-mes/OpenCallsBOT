"""Housekeeping: the feed and the database stay small.

Run at the end of every refresh:

1. Calls without a deadline that left their source (missing from two complete
   listings, see agents.Run) or not seen for `unseen_days` are over. Calls
   with a deadline end on it (vs/expiry.py).
2. What you didn't save or apply to is removed once it is over, gone, not a
   call, a duplicate, or far from your work for `low_score_days`. A removed
   call keeps a small trace (address, title, dates, score) with status
   'removed', so its page is never downloaded or read again.
3. Saved calls and applications are never removed: once over they are the
   Archive, your record of what you applied to and how it went.
4. Saved web pages older than `cache_days` go, and the database file is
   compacted (VACUUM) when that frees a lot of space.

Settings: [housekeeping] in preferences.toml."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from vs import db, present

DEFAULTS = {"low_score_days": 21, "gone_after_runs": 2, "unseen_days": 60, "cache_days": 14}
# Long columns a removed call doesn't need: only what dedupe and the source statistics use stays.
CLEARED = {"raw_text": None, "summary_en": None, "conditions": "{}", "eligibility": "{}", "required_docs": "[]",
           "submission": "{}", "other_dates": "[]", "field_confidence": "{}", "source_urls": "[]"}
KEPT = "(SELECT 1 FROM user_state u WHERE u.opportunity_key = o.dedup_key AND (u.favourite = 1 OR u.app_status IS NOT NULL))"


def settings(prefs: dict) -> dict:
    return {**DEFAULTS, **{k: v for k, v in prefs.get("housekeeping", {}).items() if k in DEFAULTS}}


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def plan(conn, prefs: dict) -> dict[str, list[int]]:
    """What a clean would do, without doing it: {reason: [opportunity ids]}."""
    s = settings(prefs)
    gone = [r[0] for r in conn.execute(
        # only calls without a deadline: a feed showing its latest posts drops
        # calls that are still open, and a deadline already says when one ends
        f"""SELECT o.id FROM opportunity o WHERE o.status = 'open' AND COALESCE(o.deadline, '') = '' AND (
              COALESCE(o.missed_runs, 0) >= ? OR (COALESCE(o.last_seen, o.first_seen, '') != '' AND COALESCE(o.last_seen, o.first_seen) < ?))""",
        (int(s["gone_after_runs"]), _ago(s["unseen_days"])))]
    removable = f"o.status != 'removed' AND NOT EXISTS {KEPT}"
    closed = [r[0] for r in conn.execute(f"SELECT o.id FROM opportunity o WHERE o.status = 'expired' AND {removable}")]
    noise = [r[0] for r in conn.execute(
        f"SELECT o.id FROM opportunity o WHERE o.status IN ('not_opportunity', 'duplicate') AND {removable}")]
    far = [r[0] for r in conn.execute(
        f"""SELECT o.id FROM opportunity o JOIN match m ON m.opportunity_id = o.id
            WHERE o.status = 'open' AND {removable} AND m.final IS NOT NULL AND m.final < ? AND o.first_seen < ?""",
        (present.far_below(), _ago(s["low_score_days"])))]
    gone_removed = [i for i in gone if not conn.execute(
        f"SELECT 1 FROM opportunity o WHERE o.id = ? AND EXISTS {KEPT}", (i,)).fetchone()]
    far = [i for i in far if i not in set(gone)]
    return {"gone": gone, "gone_removed": gone_removed, "closed": closed, "noise": noise, "far": far}


def _remove(conn, ids: list[int]) -> None:
    if not ids:
        return
    sets = ", ".join(f"{k} = ?" for k in CLEARED)
    keys = []
    for i in ids:
        conn.execute(f"UPDATE opportunity SET status = 'removed', {sets} WHERE id = ?", (*CLEARED.values(), i))
        row = conn.execute("SELECT dedup_key FROM opportunity WHERE id = ?", (i,)).fetchone()
        if row:
            keys.append(row[0])
    conn.executemany("DELETE FROM embedding WHERE kind = 'opportunity' AND key = ?", [(k,) for k in keys])


def cache_size(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM http_cache").fetchone()[0]


def clean(conn, prefs: dict, dry_run: bool = False) -> dict:
    """Do the clean (or only count it). Returns the counts, also appended
    to kv `cleanup_log` for the monthly recap."""
    s = settings(prefs)
    p = plan(conn, prefs)
    cutoff = _ago(s["cache_days"])
    old_cache = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(body)), 0) FROM http_cache WHERE fetched_at < ?", (cutoff,)).fetchone()
    out = {"at": db.now(), "gone": len(p["gone"]), "closed": len(p["closed"]) + len(p["gone_removed"]),
           "noise": len(p["noise"]), "far": len(p["far"]), "cache_pages": old_cache[0], "cache_bytes": old_cache[1]}
    out["removed"] = out["closed"] + out["noise"] + out["far"]
    if dry_run:
        return out
    conn.executemany("UPDATE opportunity SET status = 'expired' WHERE id = ?", [(i,) for i in p["gone"]])
    _remove(conn, p["closed"] + p["gone_removed"] + p["noise"] + p["far"])
    conn.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cutoff,))
    log = (db.kv_get(conn, "cleanup_log") or [])[-200:]
    db.kv_set(conn, "cleanup_log", log + [out])
    return out


def describe(c: dict) -> str:
    return (f"Cleaned: {c['removed']} removed (closed {c['closed']}, not calls or duplicates {c['noise']}, "
            f"far from your work {c['far']}), {c['gone']} gone from their source, "
            f"{c['cache_pages']} saved pages ({c['cache_bytes'] / 1e6:.0f} MB) dropped")


def compact(path, freed_bytes: int, force: bool = False) -> bool:
    """VACUUM when a lot was freed, or once a month: SQLite never gives
    space back to the disk on its own. Needs no other writer, so it runs
    after the refresh, on its own connection."""
    conn = sqlite3.connect(path, timeout=60)
    try:
        last = conn.execute("SELECT v FROM kv WHERE k = 'last_vacuum'").fetchone()
        month_ago = _ago(30)
        if not (force or freed_bytes > 50e6 or not last or last[0].strip('"') < month_ago):
            return False
        conn.execute("VACUUM")
        conn.execute("INSERT INTO kv(k, v) VALUES('last_vacuum', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (db.dumps(db.now()),))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False  # busy (the app is reading): next time
    finally:
        conn.close()


# ---------------------------------------------------------------- archive and recap

OUTCOME_GROUPS = [("selected", "Selected"), ("rejected", "Rejected"), ("no_answer", "No answer"),
                  ("submitted", "Sent, waiting for an answer"), ("not_applied", "Saved, not applied")]


def archive(conn) -> dict[str, list[dict]]:
    """Your record: calls you saved or applied to that are over, and every
    application you sent, grouped by how it went."""
    rows = [db.row_dict(r) for r in conn.execute(
        """SELECT o.id, o.title_en, o.title_original, o.org_name, o.deadline, o.deadline_kind, o.canonical_url, o.status,
                  o.type, u.app_status, u.notes, u.updated FROM opportunity o
           JOIN user_state u ON u.opportunity_key = o.dedup_key
           WHERE (u.favourite = 1 OR u.app_status IS NOT NULL) AND COALESCE(u.dismissed, 0) = 0 AND o.status != 'removed'
           ORDER BY COALESCE(NULLIF(o.deadline, ''), substr(u.updated, 1, 10)) DESC""")]
    out = {g: [] for g, _ in OUTCOME_GROUPS}
    today = datetime.now().date().isoformat()
    for r in rows:
        closed = r["status"] != "open" or bool(r["deadline"] and r["deadline"][:10] < today)
        st = r["app_status"]
        if st in ("selected", "rejected", "no_answer", "submitted"):
            out[st].append(r)
        elif closed:
            out["not_applied"].append(r)
    return out


def source_stats(conn, days: int = 60) -> dict[str, dict]:
    """Per source, over the calls first found in the last `days`: how many
    were scored, their average match (0-100) and how many you kept."""
    out = {}
    for r in conn.execute(
            f"""SELECT o.source_id sid, COUNT(m.final) n, AVG(m.final) avg, SUM(CASE WHEN EXISTS {KEPT} THEN 1 ELSE 0 END) kept
                FROM opportunity o LEFT JOIN match m ON m.opportunity_id = o.id
                WHERE o.first_seen >= ? AND o.status NOT IN ('not_opportunity', 'duplicate') GROUP BY o.source_id""", (_ago(days),)):
        out[r["sid"]] = {"n": r["n"], "avg": round(r["avg"] * 100) if r["avg"] is not None else None, "kept": r["kept"] or 0}
    return out


def pause_suggestions(conn, prefs: dict, stats: dict | None = None) -> list[str]:
    """Sources that bring mostly noise: three complete refreshes, an average
    match under 35 and nothing kept. Only suggested: you decide."""
    stats = stats if stats is not None else source_stats(conn)
    runs = {r["source_id"]: len(db.row_dict(r)["items_history"] or []) for r in conn.execute("SELECT source_id, items_history FROM source_state")}
    return [sid for sid, cfg in prefs.get("sources", {}).items()
            if cfg.get("enabled", True) and runs.get(sid, 0) >= 3 and (s := stats.get(sid)) and s["n"] >= 5
            and s["avg"] is not None and s["avg"] < 35 and not s["kept"]]


def recap(conn, prefs: dict) -> dict:
    """The monthly recap: what to settle together, and what the app cleaned."""
    arch = archive(conn)
    since = db.kv_get(conn, "recap_done_at") or ""
    log = [c for c in db.kv_get(conn, "cleanup_log") or [] if c["at"] > since]
    cleaned = {k: sum(c.get(k, 0) for c in log) for k in ("removed", "closed", "noise", "far", "gone", "cache_bytes")}
    reviewed = set(db.kv_get(conn, "recap_reviewed") or [])
    return {"waiting": arch["submitted"], "closed_unsent": [r for r in arch["not_applied"] if r["id"] not in reviewed],
            "cleaned": cleaned, "since": since, "pause": pause_suggestions(conn, prefs)}


def recap_due(conn) -> bool:
    """Once a month: due when this month's recap isn't done yet."""
    return db.kv_get(conn, "recap_month") != datetime.now().strftime("%Y-%m")


def recap_done(conn, kept_ids: list[int]) -> None:
    db.kv_set(conn, "recap_month", datetime.now().strftime("%Y-%m"))
    db.kv_set(conn, "recap_done_at", db.now())
    db.kv_set(conn, "recap_reviewed", sorted(set(db.kv_get(conn, "recap_reviewed") or []) | set(kept_ids)))
