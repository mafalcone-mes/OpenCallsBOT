"""Markdown (+ PDF) report per refresh run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import markdown as md
from jinja2 import Environment, FileSystemLoader

from vs import db, present
from vs.config import OUTPUT_DIR, RESOURCE_DIR
from vs.pdf_export import ExportError, html_to_pdf


FEED_FROM = """FROM opportunity o LEFT JOIN match m ON m.opportunity_id = o.id
              LEFT JOIN user_state u ON u.opportunity_key = o.dedup_key
              WHERE o.extraction_status = 'done' AND (
                    -- open and not past its deadline (checked on every load, not only at refresh)
                    (o.status = 'open' AND (COALESCE(o.deadline, '') = '' OR o.deadline >= date('now')))
                    -- closed, but you saved it: you may have applied and be waiting
                    OR (o.status IN ('open', 'expired') AND (u.favourite = 1 OR u.app_status IS NOT NULL))
                  )"""


FILTERS = ["type", "region", "remote", "money", "nofee", "housing", "within", "lang", "elig", "source", "status", "min", "q"]


def filter_sql(f: dict) -> tuple[str, list]:
    """The feed filters (and the chat's searches) as SQL on FEED_FROM."""
    where, params = [], []
    if f.get("type"):
        where.append("o.type = ?"); params.append(f["type"])
    if f.get("region"):
        where.append("o.region = ?"); params.append(f["region"])
    if f.get("remote"):
        where.append("o.remote_mode IN ('remote','online')" if f["remote"] == "yes" else "o.remote_mode NOT IN ('remote','online')")
    if f.get("money"):
        where.append("EXISTS (SELECT 1 FROM json_each(o.conditions, '$.money_kind') WHERE value = ?)")
        params.append(f["money"])
    if f.get("nofee"):
        where.append("NOT EXISTS (SELECT 1 FROM json_each(o.conditions, '$.money_kind') WHERE value = 'fee_required') "
                     "AND COALESCE(json_extract(o.conditions, '$.application_fee'), 0) = 0")
    if f.get("housing"):
        where.append("EXISTS (SELECT 1 FROM json_each(o.conditions, '$.covers') WHERE value = 'housing')")
    if f.get("within"):
        where.append("o.deadline != '' AND o.deadline <= date('now', ?)"); params.append(f"+{int(f['within'])} day")
    if f.get("lang"):
        where.append("EXISTS (SELECT 1 FROM json_each(o.application_languages) WHERE lower(value) = ?)")
        params.append(f["lang"])
    if f.get("elig"):
        where.append("m.eligibility_flag = ?"); params.append(f["elig"])
    if f.get("source"):
        where.append("o.source_id = ?"); params.append(f["source"])
    if f.get("status") == "favourite":
        where.append("u.favourite = 1")
    elif f.get("status") == "dismissed":
        where.append("u.dismissed = 1")
    elif f.get("status") == "tracked":
        where.append("u.app_status IS NOT NULL")
    if f.get("status") != "dismissed":
        where.append("COALESCE(u.dismissed, 0) = 0")
    if f.get("min"):
        where.append("m.final >= ?"); params.append(float(f["min"]) / 100)
    if f.get("q"):
        where.append("(o.title_en LIKE ? OR o.org_name LIKE ? OR o.summary_en LIKE ?)")
        params += [f"%{f['q']}%"] * 3
    return (" AND " + " AND ".join(where)) if where else "", params


def feed_rows(conn, where: str = "", params: tuple = (), limit: int = 200, offset: int = 0) -> list[dict]:
    # Closed saved calls last, then the ones far from you (score below
    # far_below), then likely-ineligible ones (still shown, just below
    # everything you can apply to), then by score; source and region break ties.
    sql = f"""SELECT o.*, m.final, m.bucket, m.reasons, m.suggested_projects, m.eligibility_flag, m.eligibility_notes,
                     m.discovery_reason, m.cluster, m.similar_examples, m.red_flags, m.llm_fit, m.portfolio_sim, m.taste,
                     m.keyword, m.source_w, m.prelim,
                     u.favourite, u.dismissed, u.dismiss_reason, u.app_status, u.notes
              {FEED_FROM} {where}
              ORDER BY (COALESCE(o.deadline, '') != '' AND o.deadline < date('now')),
                       COALESCE(m.final < {present.far_below()}, 0),
                       COALESCE(m.eligibility_flag, '') = 'likely_ineligible',
                       m.final IS NULL, ROUND(m.final * 100) DESC, m.tiebreak DESC, o.first_seen DESC
              LIMIT {int(limit)} OFFSET {int(offset)}"""
    return [db.row_dict(r) for r in conn.execute(sql, params)]


def feed_counts(conn, where: str = "", params: tuple = ()) -> dict[str, dict[str, int]]:
    """Per opportunity type: how many rows feed_rows would return, and how
    many of those are likely ineligible."""
    sql = f"""SELECT COALESCE(o.type, '') t, COUNT(*) n,
                     SUM(COALESCE(m.eligibility_flag, '') = 'likely_ineligible') bad
              {FEED_FROM} {where} GROUP BY 1"""
    return {r["t"]: {"n": r["n"], "bad": r["bad"] or 0} for r in conn.execute(sql, params)}


def write_run_report(run_id: int, counts: dict, sources: dict, *, pdf: bool = True) -> Path:
    with db.session() as conn:
        since = conn.execute("SELECT started FROM run WHERE id=?", (run_id,)).fetchone()["started"]
        new = feed_rows(conn, "AND o.first_seen >= ? AND COALESCE(u.dismissed, 0) = 0 AND m.bucket='feed'", (since,), 25)
        disc = feed_rows(conn, "AND m.bucket='discovery' AND COALESCE(u.dismissed, 0) = 0", (), 15)
        soon = feed_rows(conn, "AND (u.favourite = 1 OR u.app_status IS NOT NULL) AND o.deadline != '' "
                               "AND o.deadline <= date('now', '+14 day')", (), 30)
    broken = {k: v for k, v in sources.items() if v.get("error")}
    env = Environment(loader=FileSystemLoader(RESOURCE_DIR / "vs" / "web" / "templates"), autoescape=False)
    env.globals.update(present=present)
    text = env.get_template("report.md.j2").render(
        when=datetime.now().strftime("%d %b %Y %H:%M"), counts=counts, sources=sources, broken=broken,
        new=new, disc=disc, soon=soon)
    out_dir = OUTPUT_DIR / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"run-{run_id:04d}-{datetime.now():%Y-%m-%d}.md"
    path.write_text(text, encoding="utf-8")
    if pdf:
        html = env.get_template("print.html.j2").render(title=f"Run {run_id}", body=md.markdown(text, extensions=["tables"]))
        html_path = path.with_suffix(".html")
        html_path.write_text(html, encoding="utf-8")
        try:
            html_to_pdf(html_path, path.with_suffix(".pdf"))
        except ExportError:
            pass
    return path
