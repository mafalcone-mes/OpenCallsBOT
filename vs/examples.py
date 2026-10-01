"""Hand-curated taste data, stored as readable files:
  data/examples/<date>-<slug>.md   one example: YAML frontmatter + raw text
  data/signals.jsonl               append-only log of favourite/dismiss/status/note
SQLite user_state is a projection of the log and can be rebuilt from it."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from vs import db
from vs.config import EXAMPLES_DIR, SIGNALS_PATH, prefs

TASTE_DISMISS_REASONS = {"not_my_field", "not_my_taste", "too_corporate", "wrong_seniority"}
NEUTRAL_DISMISS_REASONS = {"timing", "location", "money", "ineligible", "duplicate", "other"}


@dataclass
class Example:
    id: str
    label: str            # interested | not_interested
    why: str
    added: str
    url: str
    text: str
    pinned: bool = False
    extracted: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def sign(self) -> int:
        return 1 if self.label == "interested" else -1


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "example"


def save_example(label: str, text: str, url: str = "", why: str = "", title: str = "",
                 extracted: dict | None = None) -> Example:
    today = date.today().isoformat()
    base = f"{today}-{_slug(title or url or text[:40])}"
    path = EXAMPLES_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = EXAMPLES_DIR / f"{base}-{n}.md"
        n += 1
    ex = Example(id=path.stem, label=label, why=why.strip(), added=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 url=url.strip(), text=text.strip(), extracted=extracted or {}, path=path)
    write_example(ex)
    return ex


def write_example(ex: Example) -> None:
    meta = {"id": ex.id, "label": ex.label, "why": ex.why, "added": ex.added, "url": ex.url,
            "pinned": ex.pinned, "extracted": ex.extracted}
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=100)
    ex.path.write_text(f"---\n{front}---\n\n{ex.text}\n", encoding="utf-8")


def read_example(path: Path) -> Example | None:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n?(.*)", raw, re.S)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    return Example(id=meta.get("id") or path.stem, label=meta.get("label", "interested"), why=meta.get("why") or "",
                   added=str(meta.get("added") or ""), url=meta.get("url") or "", text=m.group(2).strip(),
                   pinned=bool(meta.get("pinned")), extracted=meta.get("extracted") or {}, path=path)


def list_examples() -> list[Example]:
    out = []
    for p in sorted(EXAMPLES_DIR.glob("*.md")):
        ex = read_example(p)
        if ex:
            out.append(ex)
    return out


def get_example(example_id: str) -> Example | None:
    return next((e for e in list_examples() if e.id == example_id), None)


def remove_example(example_id: str) -> None:
    ex = get_example(example_id)
    if not ex:
        return
    removed = EXAMPLES_DIR / "_removed"
    removed.mkdir(exist_ok=True)
    shutil.move(str(ex.path), removed / ex.path.name)
    log_signal("example_removed", example_id=example_id)


def examples_hash() -> str:
    import hashlib
    h = hashlib.sha256()
    for p in sorted(EXAMPLES_DIR.glob("*.md")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- signals

def log_signal(kind: str, **data) -> dict:
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data}
    with open(SIGNALS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    with db.session() as conn:
        _apply(conn, event)
    return event


def read_signals() -> list[dict]:
    if not SIGNALS_PATH.exists():
        return []
    out = []
    for line in SIGNALS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _apply(conn, e: dict) -> None:
    key = e.get("key")
    if not key:
        return
    conn.execute("INSERT OR IGNORE INTO user_state(opportunity_key, updated) VALUES(?, ?)", (key, e["ts"]))
    kind = e["kind"]
    if kind == "favourite":
        conn.execute("UPDATE user_state SET favourite=1, dismissed=0, updated=? WHERE opportunity_key=?", (e["ts"], key))
    elif kind == "unfavourite":
        conn.execute("UPDATE user_state SET favourite=0, updated=? WHERE opportunity_key=?", (e["ts"], key))
    elif kind == "dismiss":
        conn.execute("UPDATE user_state SET dismissed=1, favourite=0, dismiss_reason=?, updated=? WHERE opportunity_key=?",
                     (e.get("reason"), e["ts"], key))
    elif kind == "undismiss":
        conn.execute("UPDATE user_state SET dismissed=0, dismiss_reason=NULL, updated=? WHERE opportunity_key=?", (e["ts"], key))
    elif kind == "status":
        conn.execute("UPDATE user_state SET app_status=?, updated=? WHERE opportunity_key=?", (e.get("status") or None, e["ts"], key))
    elif kind == "note":
        conn.execute("UPDATE user_state SET notes=?, updated=? WHERE opportunity_key=?", (e.get("text", ""), e["ts"], key))


def rebuild_user_state() -> int:
    events = read_signals()
    with db.session() as conn:
        conn.execute("DELETE FROM user_state")
        for e in events:
            _apply(conn, e)
    return len(events)


# ---------------------------------------------------------------- weighting

def _age_days(ts: str) -> float:
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400)


def decayed(weight: float, half_life: float, ts: str, pinned: bool = False) -> float:
    if pinned or not half_life:
        return weight
    return weight * 0.5 ** (_age_days(ts) / half_life)


def taste_signals() -> list[dict]:
    """Every taste signal with its effective (signed, decayed) weight.
    Returns [{"ref": "example:<id>" | "opp:<dedup_key>", "w": float, "why": str, "label": str}]"""
    cfg = prefs().get("signals", {})
    ex_cfg = cfg.get("example", {"weight": 1.0, "half_life": 365})
    out = []
    for ex in list_examples():
        w = decayed(float(ex_cfg["weight"]), float(ex_cfg["half_life"]), ex.added, ex.pinned) * ex.sign
        out.append({"ref": f"example:{ex.id}", "w": w, "why": ex.why, "label": ex.label,
                    "title": ex.extracted.get("title_en") or ex.url or ex.text[:60]})

    # Implicit signals: the latest state per opportunity wins.
    latest: dict[str, dict] = {}
    for e in read_signals():
        key = e.get("key")
        if not key:
            continue
        kind = e["kind"]
        state = latest.setdefault(key, {})
        if kind in ("favourite", "unfavourite", "dismiss", "undismiss"):
            state["pref"] = e
        elif kind == "status":
            state["status"] = e
    for key, st in latest.items():
        pref, status = st.get("pref"), st.get("status")
        if status and status.get("status") in ("submitted", "selected", "rejected", "no_answer"):
            c = cfg.get("applied", {"weight": 0.8, "half_life": 365})
            out.append({"ref": f"opp:{key}", "w": decayed(c["weight"], c["half_life"], status["ts"]), "why": "", "label": "applied"})
            continue
        if not pref:
            continue
        if pref["kind"] == "favourite":
            c = cfg.get("favourite", {"weight": 0.6, "half_life": 180})
            out.append({"ref": f"opp:{key}", "w": decayed(c["weight"], c["half_life"], pref["ts"]), "why": "", "label": "favourite"})
        elif pref["kind"] == "dismiss" and pref.get("reason") in TASTE_DISMISS_REASONS:
            c = cfg.get("dismiss_taste", {"weight": -0.5, "half_life": 180})
            out.append({"ref": f"opp:{key}", "w": decayed(c["weight"], c["half_life"], pref["ts"]),
                        "why": pref.get("reason", "").replace("_", " "), "label": "dismissed"})
    return out
