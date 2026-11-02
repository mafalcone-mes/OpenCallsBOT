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

from vs import db, embed, llm, present
from vs import portfolio as portfolio_mod
from vs.config import OUTPUT_DIR, RESOURCE_DIR, prefs
from vs.ics import to_ics
from vs.pdf_export import ExportError, export_portfolio, html_to_pdf

SELECT_SYSTEM = """You help a creative professional choose which of their projects to present for an application.
Pick 2 to 4 project angles (id = slug:category) that best answer what the call asks for. Use at most one angle per project.
For each: why it fits this call specifically, and which aspect to stress (method, theme, format, collaboration, outcome).
Order them as they should appear in the portfolio (strongest first). Only use ids from the list."""


def _select_projects(portfolio, o: dict, ranked: list[str]) -> list[dict]:
    keys = [a.key for a in portfolio.angles]
    schema = {
        "type": "object",
        "properties": {"selected": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
            "type": "object",
            "properties": {"angle": {"type": "string", "enum": keys}, "why": {"type": "string"},
                           "stress": {"type": "string"}},
            "required": ["angle", "why", "stress"]}}},
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
{chr(10).join(lines)}"""
    out = llm.chat_json(SELECT_SYSTEM, user, schema, temperature=0.1)
    seen, picked = set(), []
    for s in out.get("selected", []):
        slug = s["angle"].split(":")[0]
        if s["angle"] in keys and slug not in seen:
            seen.add(slug)
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
    n = len(selected)
    budget = limits.get("pages")
    fixed = int(cover) + int(about)
    per_angle = None
    if budget:
        # each angle: 1 opening page + roughly 1 page per 1.5 images
        spare = max(0, budget - fixed - n)
        per_angle = max(0, math.floor(spare / max(1, n) * 1.5))
    if limits.get("mb") and limits["mb"] <= 5:
        per_angle = min(per_angle if per_angle is not None else 3, 3)
    plan = []
    for s in selected:
        a = portfolio.angle(s["angle"])
        want = min(a.image_count, 6 if per_angle is None else per_angle)
        s["images"] = want
        plan.append(f"{a.slug}:{a.category}:{want}")
    return plan


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


def generate(opportunity_id: int, *, make_pdf: bool = True, log=print) -> dict:
    with db.session() as conn:
        o = db.row_dict(conn.execute("SELECT * FROM opportunity WHERE id=?", (opportunity_id,)).fetchone())
        m = db.row_dict(conn.execute("SELECT * FROM match WHERE opportunity_id=?", (opportunity_id,)).fetchone()) or {}
        if not o:
            raise ValueError(f"No opportunity {opportunity_id}")
        portfolio = portfolio_mod.load()
        model = llm.embed_model()
        angles = embed.angle_vectors(conn, portfolio, model)
        ov = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o)}, model)[o["dedup_key"]]
    ranked = sorted(angles, key=lambda k: float(angles[k] @ ov), reverse=True)

    log("Choosing projects")
    selected = _select_projects(portfolio, o, ranked[:8])
    if not selected:
        selected = [{"angle": k, "why": "closest to the call by content", "stress": ""} for k in ranked[:2]]
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
    env = Environment(loader=FileSystemLoader(RESOURCE_DIR / "vs" / "web" / "templates"), autoescape=False)
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
