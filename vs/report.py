"""Markdown (+ PDF) report per refresh run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import markdown as md
from jinja2 import Environment, FileSystemLoader

from vs import db, present
from vs.config import APP_DIR, OUTPUT_DIR
from vs.pdf_export import ExportError, html_to_pdf


def feed_rows(conn, where: str = "", params: tuple = (), limit: int = 200) -> list[dict]:
    sql = f"""SELECT o.*, m.final, m.bucket, m.reasons, m.suggested_projects, m.eligibility_flag, m.eligibility_notes,
                     m.discovery_reason, m.cluster, m.similar_examples, m.red_flags, m.llm_fit, m.portfolio_sim, m.taste,
                     m.keyword, m.source_w, m.prelim,
                     u.favourite, u.dismissed, u.dismiss_reason, u.app_status, u.notes
              FROM opportunity o LEFT JOIN match m ON m.opportunity_id = o.id
              LEFT JOIN user_state u ON u.opportunity_key = o.dedup_key
              WHERE o.extraction_status = 'done' AND (
                    -- open and not past its deadline (checked on every load, not only at refresh)
                    (o.status = 'open' AND (COALESCE(o.deadline, '') = '' OR o.deadline >= date('now')))
                    -- closed, but you saved it: you may have applied and be waiting
                    OR (o.status IN ('open', 'expired') AND (u.favourite = 1 OR u.app_status IS NOT NULL))
                  ) {where}
              ORDER BY (COALESCE(o.deadline, '') != '' AND o.deadline < date('now')), m.final IS NULL, m.final DESC, o.first_seen DESC LIMIT {int(limit)}"""
    return [db.row_dict(r) for r in conn.execute(sql, params)]


def write_run_report(run_id: int, counts: dict, sources: dict, *, pdf: bool = True) -> Path:
    with db.session() as conn:
        since = conn.execute("SELECT started FROM run WHERE id=?", (run_id,)).fetchone()["started"]
        new = feed_rows(conn, "AND o.first_seen >= ? AND COALESCE(u.dismissed, 0) = 0 AND m.bucket='feed'", (since,), 25)
        disc = feed_rows(conn, "AND m.bucket='discovery' AND COALESCE(u.dismissed, 0) = 0", (), 15)
        soon = feed_rows(conn, "AND (u.favourite = 1 OR u.app_status IS NOT NULL) AND o.deadline != '' "
                               "AND o.deadline <= date('now', '+14 day')", (), 30)
    broken = {k: v for k, v in sources.items() if v.get("error")}
    env = Environment(loader=FileSystemLoader(APP_DIR / "vs" / "web" / "templates"), autoescape=False)
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
