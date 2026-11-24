"""Application brief: projects to present and from which angle, a checklist
of what the call asks for, the deadlines, and a tailored portfolio PDF.
No cover letters are drafted."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import markdown as md
from jinja2 import Environment, FileSystemLoader

from vs import choices, db, embed, llm, present
from vs import portfolio as portfolio_mod
from vs.config import APP_DIR, OUTPUT_DIR, prefs
from vs.ics import to_ics
from vs.pdf_export import ExportError, export_portfolio, html_to_pdf

SELECT_SYSTEM = """You help a creative professional choose which of their projects to present for an application.
Pick 2 to 4 project angles (id = slug:category) that best answer what the call asks for. Use at most one angle per project.
For each: why it fits this call specifically, which aspect to stress (method, theme, format, collaboration, outcome),
and its importance: "main" (the lead project, gets the most pages), "supporting" or "brief" (one image).
Order them as they should appear in the portfolio (strongest first). Only use ids from the list.
If THEIR OWN RULES or HOW THEY BUILT IT FOR SIMILAR CALLS are given, follow them: they are how this person chooses."""


def _select_projects(portfolio, o: dict, ranked: list[str], learned: str = "") -> list[dict]:
    keys = [a.key for a in portfolio.angles]
    schema = {
        "type": "object",
        "properties": {"selected": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
            "type": "object",
            "properties": {"angle": {"type": "string", "enum": keys}, "why": {"type": "string"},
                           "stress": {"type": "string"}, "importance": {"type": "string", "enum": list(choices.LEVELS)}},
            "required": ["angle", "why", "stress", "importance"]}}},
        "required": ["selected"],
    }
    lines = []
    for key in ranked:
        a = portfolio.angle(key)
        lines.append(f"[{a.key}] {a.project_name} as {portfolio.category(a.category).label} "
                     f"({a.image_count} images): {a.subtitle}. {a.body[:700]}")
    user = f"""CALL
{o.get('title_en')} ({o.get('org_name')}), {present.TYPE_LABELS.get(o.get('type'), o.get('type'))}
{o.get('summary_en')}
Asked documents: {json.dumps(o.get('required_docs') or [], ensure_ascii=False)}
Excerpt: {(o.get('raw_text') or '')[:3000]}

PROJECT ANGLES (most similar first)
{chr(10).join(lines)}
{chr(10) + learned if learned else ''}"""
    out = llm.chat_json(SELECT_SYSTEM, user, schema, temperature=0.1)
    seen, picked = set(), []
    for s in out.get("selected", []):
        slug = s["angle"].split(":")[0]
        if s["angle"] in keys and slug not in seen:
            seen.add(slug)
            if s.get("importance") not in choices.LEVELS:
                s["importance"] = "main" if not picked else "supporting"
            picked.append(s)
    return picked


def _limits(o: dict) -> dict:
    """Page / size limits for the portfolio mentioned in the call."""
    text = " ".join(d.get("details", "") for d in o.get("required_docs") or [] if d.get("kind") in ("portfolio", "work_samples"))
    text += " " + (o.get("raw_text") or "")
    pages = re.search(r"(?:max(?:imum)?\.?|up to|fino a|massimo|maximaal|max\.?|bis zu|hasta)\s*(\d{1,2})\s*(?:pages|pagine|pagina's|pagina|seiten|páginas)", text, re.I)
    mb = re.search(r"(\d{1,3})\s*(?:MB|mb|Mb)\b", text)
    return {"pages": int(pages.group(1)) if pages else None, "mb": int(mb.group(1)) if mb else None}


def _image_plan(portfolio, selected: list[dict], limits: dict, cover: bool, about: bool) -> list[str]:
    """Images per project from its importance (main = all, supporting = 3,
    brief = 1), then shrunk to fit the call's page / size limits, taking from
    the least important projects first."""
    want = []
    for s in selected:
        a = portfolio.angle(s["angle"])
        cap = choices.LEVEL_IMAGES.get(s.get("importance") or "supporting", 3)
        want.append(min(a.image_count, choices.MAX_IMAGES if cap is None else cap))
    if limits.get("mb") and limits["mb"] <= 5:
        want = [min(w, 3) for w in want]
    budget = limits.get("pages")
    if budget:
        # each project: 1 opening page + roughly 1 page per 1.5 images
        spare = max(0, budget - int(cover) - int(about) - len(selected))
        room = math.floor(spare * 1.5)
        rank = {"brief": 0, "supporting": 1, "main": 2}
        order = sorted(range(len(selected)), key=lambda i: (rank.get(selected[i].get("importance"), 1), -i))
        while sum(want) > room:
            # one image less from the least important project that has more than one, then from any
            i = next((i for i in order if want[i] > 1), next((i for i in order if want[i] > 0), None))
            if i is None:
                break
            want[i] -= 1
    plan = []
    for s, w in zip(selected, want):
        a = portfolio.angle(s["angle"])
        s["images"] = w
        plan.append(f"{a.slug}:{a.category}:{w}")
    return plan


def editor_rows(portfolio, selected: list[dict], ranked: list[str]) -> list[dict]:
    """Every project for the portfolio editor: the chosen ones first in their
    order, then the others (closest to the call first), each with its
    categories to choose from."""
    by_slug = {s["angle"].split(":", 1)[0]: s for s in selected}
    order = [s["angle"].split(":", 1)[0] for s in selected]
    for key in ranked:
        slug = key.split(":", 1)[0]
        if slug not in order:
            order.append(slug)
    for proj in portfolio.projects:
        if proj.slug not in order:
            order.append(proj.slug)
    rows = []
    for slug in order:
        angles = [a for a in portfolio.angles if a.slug == slug]
        if not angles:
            continue
        chosen = by_slug.get(slug)
        best = next((k for k in ranked if k.split(":", 1)[0] == slug), angles[0].key)
        proj = portfolio.project(slug)
        rows.append({
            "slug": slug, "name": angles[0].project_name, "year": proj.year if proj else None,
            "shown": bool(chosen), "angle": chosen["angle"] if chosen else best,
            "importance": chosen.get("importance", "supporting") if chosen else "supporting",
            "categories": [(a.key, portfolio.category(a.category).label, a.image_count) for a in angles],
            "why": chosen.get("why", "") if chosen else "",
        })
    return rows


def checklist(o: dict, portfolio, me: dict) -> list[dict]:
    items = []
    my_langs = {k.lower() for k in (me.get("languages") or {})}
    for d in o.get("required_docs") or []:
        kind = d.get("kind", "other")
        status = "to write yourself"
        if kind == "portfolio" or kind == "work_samples":
            status = "generated below (portfolio.pdf)"
        elif kind == "cv":
            status = ("available (uploaded cv.pdf)" if "cv" in portfolio.uploaded else "available (portfolio cv.pdf)"
                      ) if portfolio.has_cv else "missing: add cv.pdf to your portfolio, or upload it on Home"
        elif kind in ("id_document", "diploma"):
            status = "gather"
        items.append({"label": present.DOC_LABELS.get(kind, kind), "details": d.get("details", ""),
                      "required": d.get("required", True), "status": status})
    sub = o.get("submission") or {}
    if sub.get("method") and sub["method"] != "unknown":
        items.append({"label": "Submission", "details": f"{sub['method']} {sub.get('url', '')}".strip(), "required": True,
                      "status": ""})
    langs = o.get("application_languages") or []
    if langs:
        ok = bool({l.lower() for l in langs} & my_langs)
        items.append({"label": "Application language", "details": ", ".join(langs), "required": True,
                      "status": "ok" if ok else "not one of your languages in preferences"})
    cond = o.get("conditions") or {}
    if cond.get("application_fee"):
        items.append({"label": "Application fee", "details": present.money(cond["application_fee"], cond.get("currency", "")),
                      "required": True, "status": "to pay"})
    if sub.get("anonymous"):
        items.append({"label": "Anonymous submission", "details": "Portfolio generated without cover and about pages; check images for names.",
                      "required": True, "status": "check"})
    return items


def _context(opportunity_id: int, learn: bool = True):
    with db.session() as conn:
        o = db.row_dict(conn.execute("SELECT * FROM opportunity WHERE id=?", (opportunity_id,)).fetchone())
        m = db.row_dict(conn.execute("SELECT * FROM match WHERE opportunity_id=?", (opportunity_id,)).fetchone()) or {}
        if not o:
            raise ValueError(f"No opportunity {opportunity_id}")
        portfolio = portfolio_mod.load()
        model = llm.embed_model()
        angles = embed.angle_vectors(conn, portfolio, model)
        ov = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o)}, model)[o["dedup_key"]]
        learned = choices.prompt_block(conn, o, portfolio, model) if learn else ""
    ranked = sorted(angles, key=lambda k: float(angles[k] @ ov), reverse=True)
    return o, m, portfolio, ranked, learned


def propose(opportunity_id: int, *, log=print) -> dict:
    """The app's pick (projects, order, importance), using what it learned
    from your past choices. Returns {selected, ranked}."""
    o, _, portfolio, ranked, learned = _context(opportunity_id)
    log("Choosing projects" + (", following what you taught it" if learned else ""))
    selected = _select_projects(portfolio, o, ranked[:8], learned)
    if not selected:
        selected = [{"angle": k, "why": "closest to the call by content", "stress": "",
                     "importance": "main" if i == 0 else "supporting"} for i, k in enumerate(ranked[:2])]
    return {"selected": selected, "ranked": ranked}


def generate(opportunity_id: int, *, make_pdf: bool = True, log=print) -> dict:
    """Propose and build in one go, with no editing step (CLI, 'Brief only')."""
    return build(opportunity_id, propose(opportunity_id, log=log)["selected"], make_pdf=make_pdf, log=log)


def build(opportunity_id: int, selected: list[dict], *, make_pdf: bool = True, log=print) -> dict:
    """The brief and the portfolio PDF from the final choice of projects."""
    o, m, portfolio, _, _ = _context(opportunity_id, learn=False)
    selected = [dict(s) for s in selected if portfolio.angle(s["angle"])]
    if not selected:
        raise ValueError("No projects chosen for the portfolio")
    for s in selected:
        s.setdefault("why", "")
        s.setdefault("stress", "")
    me = prefs().get("me", {})
    anonymous = bool((o.get("submission") or {}).get("anonymous"))
    cover = about = not anonymous
    limits = _limits(o)
    plan = _image_plan(portfolio, selected, limits, cover, about)

    out_dir = OUTPUT_DIR / "briefs" / f"{o['id']}-{re.sub(r'[^a-z0-9]+', '-', (o.get('title_en') or '').lower())[:40].strip('-')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_info = {"path": None, "error": None, "manual": None}
    fmt = portfolio_mod.portfolio_format()
    if make_pdf and fmt != "website":
        # Only the website format can render a tailored portfolio (it uses the
        # site's own export script). Point to the person's own PDF instead.
        own = [str(f) for f in sorted(portfolio.root.glob("*.pdf")) if f.name.lower() != "cv.pdf"]
        pdf_info["error"] = ("A tailored portfolio PDF is made only from the website format. "
                             + (f"Use your own portfolio PDF: {own[0]}" if own else "Attach your own portfolio PDF."))
    elif make_pdf:
        log("Exporting the portfolio PDF")
        try:
            path = export_portfolio(plan, out_dir / "portfolio.pdf", cover=cover, about=about, log=log)
            pdf_info["path"] = str(path)
        except ExportError as e:
            pdf_info.update(error=str(e), manual=e.manual_command)
            log(f"Portfolio PDF failed: {e}")

    items = checklist(o, portfolio, me)
    for s in selected:
        a = portfolio.angle(s["angle"])
        s["project"] = a.project_name
        s["category"] = portfolio.category(a.category).label
        s["color"] = portfolio.category(a.category).color
    brief = {
        "o": o, "m": m, "selected": selected, "checklist": items, "limits": limits, "plan": plan,
        "pdf": pdf_info, "warnings": [w for w in portfolio.warnings if "placeholder" in w or "cv.pdf" in w],
        "money": present.money_badge(o.get("conditions") or {}),
        "deadline": present.deadline_label(o.get("deadline") or "", o.get("deadline_kind") or ""),
        "location": present.location(o), "type": present.TYPE_LABELS.get(o.get("type"), o.get("type")),
        "anonymous": anonymous,
    }
    env = Environment(loader=FileSystemLoader(APP_DIR / "vs" / "web" / "templates"), autoescape=False)
    md_text = env.get_template("brief.md.j2").render(**brief)
    (out_dir / "brief.md").write_text(md_text, encoding="utf-8")
    html = env.get_template("print.html.j2").render(title=f"Brief: {o.get('title_en')}",
                                                    body=md.markdown(md_text, extensions=["tables"]),
                                                    tokens=portfolio.css_tokens)
    (out_dir / "brief.html").write_text(html, encoding="utf-8")
    (out_dir / "deadlines.ics").write_bytes(to_ics([o]))
    brief_pdf = None
    try:
        brief_pdf = str(html_to_pdf(out_dir / "brief.html", out_dir / "brief.pdf"))
    except ExportError as e:
        log(f"brief.pdf failed: {e}")
    with db.session() as conn:
        db.kv_set(conn, f"brief:{o['id']}", {"dir": str(out_dir), "portfolio_pdf": pdf_info, "brief_pdf": brief_pdf,
                                             "selected": selected, "at": db.now()})
    return {"dir": str(out_dir), "portfolio_pdf": pdf_info, "brief_pdf": brief_pdf, "selected": selected}
