"""Can the scoring predict what you star? Leave-one-out: hide one star, score
everything with the remaining signals, see where the hidden star ranks.
Embedding level (no LLM), so it runs in seconds and can compare variants."""

from __future__ import annotations

import numpy as np

from vs import db, embed, llm
from vs import portfolio as portfolio_mod
from vs.config import prefs
from vs.examples import taste_signals
from vs.score import keyword_score


def _z(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() or 1.0)


def run(variants: dict[str, dict] | None = None) -> dict:
    p = prefs()
    w0 = p.get("weights", {})
    taste = p.get("taste", {})
    model = llm.embed_model()
    portfolio = portfolio_mod.load()
    with db.session() as conn:
        opps = [db.row_dict(r) for r in conn.execute(
            "SELECT o.*, u.favourite FROM opportunity o LEFT JOIN user_state u ON u.opportunity_key = o.dedup_key "
            "WHERE o.extraction_status = 'done' AND o.status IN ('open', 'expired')")]
        vecs = embed.ensure(conn, "opportunity", {o["dedup_key"]: embed.opportunity_text(o) for o in opps}, model)
        _, a_mat = embed.matrix(embed.angle_vectors(conn, portfolio, model))
        ref_vecs = db.all_vecs(conn, "reference", model)
        ex_vecs = db.all_vecs(conn, "example", model)
        st_vecs = db.all_vecs(conn, "statement", model)
    keys = [o["dedup_key"] for o in opps]
    M = np.vstack([vecs[k] for k in keys])
    P = _z(np.sort(M @ a_mat.T, axis=1)[:, ::-1][:, :2].mean(axis=1))
    K = np.array([keyword_score(" ".join(filter(None, [o.get("title_en"), o.get("title_original"), o.get("summary_en"),
                                                        o.get("field_tag"), (o.get("raw_text") or "")[:4000]])), taste)[0]
                  for o in opps])
    stars = [i for i, o in enumerate(opps) if o.get("favourite")]
    # non-star signals: examples, corrections, references (weights as in score.py)
    base = [s for s in taste_signals() if not s["ref"].startswith("opp:")]
    base_vecs, base_w = [], []
    for s in base:
        v = ex_vecs.get(s["ref"])
        if v is not None:
            base_vecs.append(v)
            base_w.append(s["w"])
    for v in st_vecs.values():
        base_vecs.append(v)
        base_w.append(1.0)
    refs = list(ref_vecs.values())

    variants = variants or {
        "current": {},
        "stars weigh 1.0": {"fav_w": 1.0},
        "taste x2": {"taste_max": 2.0},
        "stars 1.0 + taste x2": {"fav_w": 1.0, "taste_max": 2.0},
        "portfolio only (cold)": {"taste_max": 0.0},
    }
    out = {}
    for name, v in variants.items():
        fav_w = v.get("fav_w", p.get("signals", {}).get("favourite", {}).get("weight", 0.6))
        taste_max = v.get("taste_max", float(w0.get("taste_max", 1.0)))
        pcts = []
        for hold in stars:
            others = [i for i in stars if i != hold]
            S = base_vecs + [M[i] for i in others] + refs
            W = base_w + [fav_w] * len(others) + [float(w0.get("reference_total", 3.0)) / max(1, len(refs))] * len(refs)
            if not S:
                T = np.zeros(len(opps))
            else:
                Smat, Wv = np.vstack(S), np.array(W)
                sims = M @ Smat.T
                top = np.argsort(-sims, axis=1)[:, :5]
                ws = np.take_along_axis(np.broadcast_to(Wv, sims.shape), top, axis=1)
                T = (np.take_along_axis(sims, top, axis=1) * ws).sum(axis=1) / ws.sum(axis=1)
            n_eff = sum(abs(x) for x in W)
            wt = taste_max * min(1.0, n_eff / float(w0.get("taste_full_at", 15)))
            score = float(w0.get("portfolio", 1.0)) * P + wt * _z(T) + float(w0.get("keywords", 0.35)) * K
            mask = np.ones(len(opps), bool)
            mask[others] = False  # other stars don't compete for rank
            rank = int((score[mask] > score[hold]).sum())
            pcts.append(1 - rank / mask.sum())
        out[name] = {"median_top_pct": round(100 * (1 - float(np.median(pcts))), 1),
                     "in_top_10pct": f"{sum(p >= 0.9 for p in pcts)}/{len(pcts)}",
                     "in_top_25pct": f"{sum(p >= 0.75 for p in pcts)}/{len(pcts)}"}
    return {"stars": len(stars), "opportunities": len(opps), "variants": out}


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run(), indent=2))
