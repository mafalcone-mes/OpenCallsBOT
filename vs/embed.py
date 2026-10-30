"""Embeddings for project angles, opportunities and examples, cached in
SQLite by (kind, key, model) and invalidated by a hash of the embedded text."""

from __future__ import annotations

import hashlib

import numpy as np

from vs import db, llm
from vs.portfolio import Portfolio


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ensure(conn, kind: str, items: dict[str, str], model: str) -> dict[str, np.ndarray]:
    """Embed whatever is missing or changed; return key -> vector."""
    out, todo = {}, []
    for key, text in items.items():
        h = text_hash(text)
        vec = db.get_vec(conn, kind, key, model, h)
        if vec is None:
            todo.append((key, text, h))
        else:
            out[key] = vec
    if todo:
        vecs = llm.embed([t for _, t, _ in todo], model=model)
        for (key, _, h), vec in zip(todo, vecs):
            db.put_vec(conn, kind, key, model, h, vec)
            out[key] = vec
        # Commit now: the caller's next step is often another model call, and an
        # open write would lock every other part of the app while it runs.
        conn.commit()
    return out


def angle_vectors(conn, portfolio: Portfolio, model: str) -> dict[str, np.ndarray]:
    items = {a.key: a.embed_text(portfolio.category(a.category).label) for a in portfolio.angles}
    return ensure(conn, "angle", items, model)


def opportunity_text(o: dict) -> str:
    parts = [o.get("title_en") or o.get("title_original") or "", o.get("org_name") or "",
             o.get("field_tag") or "", o.get("summary_en") or "", (o.get("raw_text") or "")[:3000]]
    return "\n".join(p for p in parts if p)


def matrix(vecs: dict[str, np.ndarray]) -> tuple[list[str], np.ndarray]:
    keys = list(vecs)
    if not keys:
        return [], np.zeros((0, 1), dtype=np.float32)
    return keys, np.vstack([vecs[k] for k in keys])
