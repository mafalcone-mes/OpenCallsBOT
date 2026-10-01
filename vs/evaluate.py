"""Does taste learning help? Hold out some of your examples, mix them into
the real opportunities, and see where they rank: cold start (portfolio +
preferences only) vs with taste. Embedding-level only, so it runs in seconds."""

from __future__ import annotations

import random

import numpy as np

from vs import db, embed, llm
from vs import portfolio as portfolio_mod
from vs.config import prefs
from vs.examples import list_examples


def _auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def evaluate(splits: int = 5, holdout: float = 0.2, seed: int = 7) -> dict:
    exs = list_examples()
    pos_ex = [e for e in exs if e.label == "interested"]
    neg_ex = [e for e in exs if e.label != "interested"]
    if len(pos_ex) < 3:
        return {"error": "Need at least 3 'interested' examples to evaluate."}
    w = prefs().get("weights", {})
    lam = float(w.get("negative_lambda", 0.7))
    model = llm.embed_model()
    portfolio = portfolio_mod.load()
    with db.session() as conn:
        _, a_mat = embed.matrix(embed.angle_vectors(conn, portfolio, model))
        texts = {e.id: "\n".join(filter(None, [e.extracted.get("title_en"), e.extracted.get("summary_en"), e.text[:3000]]))
                 for e in exs}
        ex_vecs = embed.ensure(conn, "example", {f"example:{k}": v for k, v in texts.items()}, model)
        pool = list(db.all_vecs(conn, "opportunity", model).values())[-300:]
    if not pool:
        return {"error": "No scraped opportunities yet: run a refresh first."}
    pool_mat = np.vstack(pool)
    rng = random.Random(seed)
    results = {"cold": [], "taste": []}
    for _ in range(splits):
        hp = rng.sample(pos_ex, max(1, int(len(pos_ex) * holdout)))
        hn = rng.sample(neg_ex, max(1, int(len(neg_ex) * holdout))) if len(neg_ex) >= 2 else []
        train_pos = np.vstack([ex_vecs[f"example:{e.id}"] for e in pos_ex if e not in hp])
        train_neg = [ex_vecs[f"example:{e.id}"] for e in neg_ex if e not in hn]
        train_neg = np.vstack(train_neg) if train_neg else None
        test = [ex_vecs[f"example:{e.id}"] for e in hp + hn]
        labels = [1] * len(hp) + [0] * len(hn)
        cand = np.vstack([pool_mat] + test)

        def P(m):
            s = np.sort(m @ a_mat.T, axis=1)[:, ::-1][:, :2].mean(axis=1)
            return (s - s.mean()) / (s.std() or 1)

        def T(m):
            tp = np.sort(m @ train_pos.T, axis=1)[:, ::-1][:, :5].mean(axis=1)
            tn = np.sort(m @ train_neg.T, axis=1)[:, ::-1][:, :5].mean(axis=1) if train_neg is not None else 0
            t = tp - lam * tn
            return (t - t.mean()) / (t.std() or 1)

        for mode, scores in (("cold", P(cand)), ("taste", P(cand) + float(w.get("taste_max", 1.0)) * T(cand))):
            order = np.argsort(-scores)
            rank = np.empty_like(order)
            rank[order] = np.arange(len(order))
            n = len(cand)
            test_idx = range(len(pool), n)
            pct = [1 - rank[i] / n for i, lab in zip(test_idx, labels) if lab == 1]
            r20 = [rank[i] < 20 for i, lab in zip(test_idx, labels) if lab == 1]
            pos_s = [float(scores[i]) for i, lab in zip(test_idx, labels) if lab == 1]
            neg_s = [float(scores[i]) for i, lab in zip(test_idx, labels) if lab == 0]
            results[mode].append({"pct": float(np.mean(pct)), "recall20": float(np.mean(r20)), "auc": _auc(pos_s, neg_s)})

    def agg(rows, k):
        vals = [r[k] for r in rows if r[k] is not None]
        return round(float(np.mean(vals)), 3) if vals else None

    out = {"at": db.now(), "n_pos": len(pos_ex), "n_neg": len(neg_ex), "pool": len(pool)}
    for mode in ("cold", "taste"):
        out[mode] = {k: agg(results[mode], k) for k in ("pct", "recall20", "auc")}
    with db.session() as conn:
        hist = db.kv_get(conn, "eval_history") or []
        db.kv_set(conn, "eval_history", (hist + [out])[-30:])
    return out
