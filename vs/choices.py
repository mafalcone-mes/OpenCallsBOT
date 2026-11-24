"""How you build a portfolio for a call, and why, so the next proposal is closer.

For each call you confirm, we keep what the app proposed, what you chose
(projects, order, importance) and your notes on the differences, in
data/portfolio-choices.jsonl (personal, like your stars). Projects are kept
by id (slug:category) only: names and texts are read from the portfolio
when shown, never copied here.

Your notes are also distilled into a few general rules
(data/portfolio-rules.json), which you can read and edit in the app. When
the app proposes a portfolio, the model sees those rules and your choices
on the most similar past calls.
"""

from __future__ import annotations

import json

import numpy as np

from vs import embed, llm
from vs.config import DATA_DIR

CHOICES_PATH = DATA_DIR / "portfolio-choices.jsonl"
RULES_PATH = DATA_DIR / "portfolio-rules.json"

# Importance sets how much space a project gets: max images (None = all).
LEVELS = {"main": "Main", "supporting": "Supporting", "brief": "Brief"}
LEVEL_IMAGES = {"main": None, "supporting": 3, "brief": 1}
MAX_IMAGES = 8


def _slug(angle: str) -> str:
    return angle.split(":", 1)[0]


# ---------------------------------------------------------------- storage

def load() -> list[dict]:
    if not CHOICES_PATH.exists():
        return []
    out = []
    for line in CHOICES_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # one record per call: the latest wins
    by_call = {r.get("call_key") or r.get("oid"): r for r in out}
    return list(by_call.values())


def save(record: dict) -> None:
    CHOICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHOICES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def rules() -> list[str]:
    try:
        return [r for r in json.loads(RULES_PATH.read_text(encoding="utf-8")).get("rules", []) if r.strip()]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def set_rules(items: list[str]) -> None:
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULES_PATH.write_text(json.dumps({"rules": [r.strip() for r in items if r.strip()]}, ensure_ascii=False, indent=1),
                          encoding="utf-8")


# ---------------------------------------------------------------- what changed

def diff(proposal: list[dict], final: list[dict]) -> list[dict]:
    """What you changed from the proposal. Each item: {id, kind, angle, from, to}.
    Kinds: added, removed, category, importance, moved."""
    p_by = {_slug(s["angle"]): s for s in proposal}
    f_by = {_slug(s["angle"]): s for s in final}
    changes = []
    for s in final:
        slug = _slug(s["angle"])
        if slug not in p_by:
            changes.append({"kind": "added", "angle": s["angle"], "to": s.get("importance", "")})
            continue
        before = p_by[slug]
        if before["angle"] != s["angle"]:
            changes.append({"kind": "category", "angle": s["angle"], "from": before["angle"], "to": s["angle"]})
        if before.get("importance") != s.get("importance"):
            changes.append({"kind": "importance", "angle": s["angle"], "from": before.get("importance", ""),
                            "to": s.get("importance", "")})
    for s in proposal:
        if _slug(s["angle"]) not in f_by:
            changes.append({"kind": "removed", "angle": s["angle"]})
    # order, among the projects kept from the proposal
    kept_p = [_slug(s["angle"]) for s in proposal if _slug(s["angle"]) in f_by]
    kept_f = [_slug(s["angle"]) for s in final if _slug(s["angle"]) in p_by]
    if kept_p != kept_f:
        for slug in kept_f:
            i, j = kept_p.index(slug), kept_f.index(slug)
            if i != j:
                changes.append({"kind": "moved", "angle": f_by[slug]["angle"], "from": i + 1, "to": j + 1})
    for n, c in enumerate(changes):
        c["id"] = n
    return changes


def describe(c: dict, portfolio) -> str:
    """One change in words, with names read live from the portfolio."""
    def name(angle: str) -> str:
        a = portfolio.angle(angle)
        return a.project_name if a else _slug(angle)

    def cat(angle: str) -> str:
        return portfolio.category(angle.split(":", 1)[-1]).label
    k = c["kind"]
    if k == "added":
        return f"Added {name(c['angle'])} ({cat(c['angle'])}, {LEVELS.get(c.get('to'), c.get('to'))})"
    if k == "removed":
        return f"Removed {name(c['angle'])}"
    if k == "category":
        return f"Showed {name(c['angle'])} as {cat(c['to'])} instead of {cat(c['from'])}"
    if k == "importance":
        return f"Made {name(c['angle'])} {LEVELS.get(c['to'], c['to'])} instead of {LEVELS.get(c['from'], c['from'])}"
    if k == "moved":
        return f"Moved {name(c['angle'])} {'up' if c['to'] < c['from'] else 'down'} (from {c['from']} to {c['to']})"
    return k


def describe_list(sel: list[dict], portfolio) -> str:
    parts = []
    for s in sel:
        a = portfolio.angle(s["angle"])
        if a:
            parts.append(f"{a.project_name} as {portfolio.category(a.category).label} "
                         f"({LEVELS.get(s.get('importance'), s.get('importance', ''))})")
    return "; ".join(parts)


# ---------------------------------------------------------------- learning

def call_text(o: dict) -> str:
    return embed.opportunity_text(o)


def similar(conn, o: dict, model: str, k: int = 3, min_sim: float = 0.5) -> list[dict]:
    """Your past choices on the calls most like this one."""
    past = [r for r in load() if r.get("final") and r.get("call_key") != o.get("dedup_key")]
    if not past:
        return []
    vecs = embed.ensure(conn, "choice", {f"choice:{r['call_key']}": r.get("call_text", "") for r in past}, model)
    ov = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o)}, model)[o["dedup_key"]]
    scored = sorted(((float(np.dot(vecs[f"choice:{r['call_key']}"], ov)), r) for r in past), key=lambda t: -t[0])
    return [r for s, r in scored[:k] if s >= min_sim]


def prompt_block(conn, o: dict, portfolio, model: str) -> str:
    """Text for the project-selection prompt: your rules and similar past choices."""
    out = []
    rs = rules()
    if rs:
        out.append("THEIR OWN RULES FOR BUILDING A PORTFOLIO (follow them):\n" + "\n".join(f"- {r}" for r in rs))
    examples = []
    for r in similar(conn, o, model):
        line = f"- Call \"{r.get('title', '')}\" ({r.get('org', '')}): they chose {describe_list(r['final'], portfolio)}."
        notes = [f"{describe(c, portfolio)}{' because ' + c['why'] if c.get('why') else ''}"
                 for c in r.get("changes", []) if c.get("why") or c["kind"] in ("added", "removed")]
        if notes:
            line += " Their changes to the proposal: " + "; ".join(notes) + "."
        if r.get("note"):
            line += f" Their note: {r['note']}"
        examples.append(line)
    if examples:
        out.append("HOW THEY BUILT IT FOR SIMILAR CALLS (follow the same logic):\n" + "\n".join(examples))
    return "\n\n".join(out)


DISTILL_SYSTEM = """You help a creative professional keep a short list of their own rules for building a portfolio
for an application: which projects to show, in what order, and which to give the most space.
You get their current rules and the notes they wrote when they changed a proposed portfolio.
Return at most 8 short, general rules in their voice ("Lead with...", "Leave out... when..."). Keep every current rule
unless a note contradicts it; add a rule only when a note supports it. Never invent reasons they did not give.
Write in the language of their notes."""


def distill(portfolio) -> list[str]:
    """Rewrite the rules from all notes so far (keeps your edits unless a note contradicts them)."""
    lines = []
    for r in load():
        notes = [f"{describe(c, portfolio)}: {c['why']}" for c in r.get("changes", []) if c.get("why")]
        if r.get("note"):
            notes.append(f"overall: {r['note']}")
        if notes:
            lines.append(f"Call \"{r.get('title', '')}\": " + " | ".join(notes))
    if not lines:
        return rules()
    current = rules()
    user = ("CURRENT RULES\n" + ("\n".join(f"- {r}" for r in current) if current else "(none yet)")
            + "\n\nTHEIR NOTES\n" + "\n".join(lines[-40:]))
    schema = {"type": "object", "properties": {"rules": {"type": "array", "maxItems": 8, "items": {"type": "string"}}},
              "required": ["rules"]}
    out = llm.chat_json(DISTILL_SYSTEM, user, schema, temperature=0.1)
    new = [r for r in out.get("rules", []) if isinstance(r, str) and r.strip()]
    if new:
        set_rules(new)
    return new or current
