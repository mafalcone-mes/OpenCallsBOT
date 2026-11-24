"""A map of the calls around your work (prototype).

Your projects at the centre, the website's categories around them, and the
calls outside, each tied to the category whose projects it reads most like
(bge-m3); the closer to its category, the higher it ranks for the sort you
pick (resemblance to your projects, your taste, or analog <-> digital). A
side bar filters by the Feed's groups and categories, lists the calls and
opens one with all its details. Walk from one call to the next and save the path.

    uv run python scripts/embedding_map.py            # writes output/embedding-map.html
    VS_DATA_DIR=data-design uv run python scripts/embedding_map.py

Only reads the database (and asks Ollama to embed the guide words); personal
data stays in output/, never shared.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vs import db, llm, present  # noqa: E402
from vs.config import DATA_DIR, OUTPUT_DIR, prefs  # noqa: E402
from vs.examples import list_examples  # noqa: E402

K_LIST = 10    # most-alike texts kept for each dot

# The analog <-> digital sort: what each end means. Edit the words and run
# the script again; a call's place is how much closer it is to one end.
ANALOG = [
    "analog film photography, darkroom, printmaking, risograph, letterpress, screen printing",
    "handmade craft, ceramics, textile, weaving, woodwork, sculpture, materials",
    "set design, scenography, theatre stage, physical installation, exhibition space, architecture",
    "painting, drawing, illustration on paper, bookbinding, printed publications",
]
DIGITAL = [
    "artificial intelligence, machine learning, generative AI, algorithms",
    "creative coding, software development, interactive installation, programming",
    "data visualization, digital platforms, web design, UX UI design, apps",
    "virtual reality, augmented reality, game design, 3D, digital media, video",
]

ICONS = ["house", "newspaper", "compass", "list-todo", "archive", "sparkles", "scan-search", "sun", "moon", "monitor",
         "panel-left", "map-pin", "clock", "external-link", "star", "file-text", "x", "search", "rotate-ccw", "trash-2",
         "arrow-right", "chevron-left", "banknote", "triangle-alert", "menu"]


# ---------------------------------------------------------------- details, as display text

def _flat(v) -> str:
    if isinstance(v, dict):
        return " · ".join(str(x) for x in v.values() if x not in (None, "", [], {}))
    return str(v)


def details(r: dict) -> dict:
    """Everything shown when a call is opened, prepared here so the page stays simple."""
    el = r.get("eligibility") or {}
    elig = []
    nat = [n for n in el.get("nationalities") or [] if n]
    if nat:
        elig.append("Nationalities: " + ("any" if "all" in nat else ", ".join(nat)))
    if el.get("residence"):
        elig.append("Residence: " + ", ".join(el["residence"]))
    if el.get("age_min") or el.get("age_max"):
        elig.append(f"Age: {el.get('age_min') or '–'} to {el.get('age_max') or '–'}")
    for key, lab in (("education", "Education"), ("career_stage", "Career stage")):
        if el.get(key):
            elig.append(f"{lab}: {el[key]}")
    elig += [n for n in r.get("eligibility_notes") or [] if n]
    docs = []
    for d in r.get("required_docs") or []:
        if isinstance(d, dict):
            name = (d.get("kind") or "").replace("_", " ").capitalize()
            docs.append(f"{name}{' (optional)' if d.get('required') is False else ''}{': ' + d['details'] if d.get('details') else ''}")
        else:
            docs.append(str(d))
    sub = r.get("submission") or {}
    main, when = present.deadline_parts(r.get("deadline") or "", r.get("deadline_kind") or "")
    reasons = [x for x in r.get("reasons") or [] if x and not x.startswith("Keywords:")]
    return {
        "summary": r.get("summary_en") or "", "field": r.get("field_tag") or "",
        "money": present.money_badge(r.get("conditions") or {}), "deadline": [main, when],
        "place": present.location(r), "duration": r.get("duration_text") or "", "start": r.get("start_date") or "",
        "langs": ", ".join(r.get("application_languages") or []), "elig_flag": r.get("eligibility_flag") or "",
        "elig": elig, "docs": docs, "dates": [_flat(x) for x in r.get("other_dates") or [] if x],
        "how": " · ".join(x for x in (sub.get("method") or "", sub.get("url") or "") if x),
        "reasons": reasons, "projects": present.project_chips(r.get("suggested_projects"), n=4),
        "flags": [str(x) for x in r.get("red_flags") or [] if x],
    }


# ---------------------------------------------------------------- data

def _model(conn) -> str:
    want = prefs().get("llm", {}).get("embed_model", "bge-m3")
    rows = [r[0] for r in conn.execute("SELECT model_id, COUNT(*) n FROM embedding GROUP BY model_id ORDER BY n DESC")]
    return next((m for m in rows if m == want or m.split(":")[0] == want), rows[0] if rows else want)


def collect() -> tuple[list[dict], np.ndarray, str]:
    nodes, vecs = [], []
    today = date.today().isoformat()
    with db.session() as conn:
        model = _model(conn)
        briefs = {int(k.split(":")[1]) for (k,) in conn.execute("SELECT k FROM kv WHERE k LIKE 'brief:%'")
                  if k.split(":")[1].isdigit()}

        def add(node: dict, blob: bytes) -> None:
            nodes.append(node)
            vecs.append(np.frombuffer(blob, dtype=np.float32))

        for row in conn.execute(
                "SELECT e.vec, o.*, m.final, m.reasons, m.suggested_projects, m.eligibility_flag, m.eligibility_notes, "
                "m.red_flags, u.favourite, u.dismissed, u.app_status, u.notes "
                "FROM embedding e JOIN opportunity o ON o.dedup_key = e.key "
                "LEFT JOIN match m ON m.opportunity_id = o.id LEFT JOIN user_state u ON u.opportunity_key = o.dedup_key "
                "WHERE e.kind = 'opportunity' AND e.model_id = ? AND o.status IN ('open', 'expired')", (model,)):
            r = db.row_dict(row)
            add({"g": "opp", "t": r["type"] or "other", "grp": present.feed_group_of(r["type"]),
                 "label": r["title_en"] or r["title_original"] or "?", "sub": r["org_name"] or "", "id": r["id"],
                 "url": r["canonical_url"], "dl": r["deadline"] or "", "place": present.location(r),
                 "score": None if r["final"] is None else round(r["final"] * 100),
                 "fav": bool(r["favourite"]), "dis": bool(r["dismissed"]),
                 "closed": r["status"] != "open" or bool(r["deadline"] and r["deadline"][:10] < today),
                 "status": r["app_status"] or "", "note": (r["notes"] or "").strip()[:600], "brief": r["id"] in briefs,
                 "d": details(r)}, row["vec"])

        for r in conn.execute("SELECT key, vec FROM embedding WHERE kind = 'angle' AND model_id = ?", (model,)):
            slug, _, cat = r["key"].partition(":")
            add({"g": "project", "slug": slug, "cat": cat, "label": slug.replace("-", " ").title(), "sub": cat.replace("-", " ")}, r["vec"])

        examples = {f"example:{e.id}": e for e in list_examples()}
        for r in conn.execute("SELECT key, vec FROM embedding WHERE kind = 'example' AND model_id = ?", (model,)):
            e = examples.get(r["key"])
            title = (e.extracted.get("title_en") if e else None) or r["key"].split(":", 1)[-1][11:].replace("-", " ")
            add({"g": "liked" if e and e.label == "interested" else "disliked", "label": title,
                 "sub": (e.extracted.get("org_name") if e else "") or "", "note": (e.why if e else "") or "",
                 "url": e.url if e else ""}, r["vec"])

        for r in conn.execute("SELECT key, vec FROM embedding WHERE kind = 'reference' AND model_id = ?", (model,)):
            studio, _, proj = r["key"].split(":", 1)[-1].partition("/")
            add({"g": "ref", "label": proj.replace("-", " ").capitalize(), "sub": studio.replace("-", " ")}, r["vec"])
    x = np.vstack(vecs) if vecs else np.zeros((0, 1024), np.float32)
    return nodes, x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9), model


def top(sims: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    k = min(k, sims.shape[1] - 1)
    idx = np.argpartition(-sims, k, axis=1)[:, :k]
    order = np.take_along_axis(-sims, idx, axis=1).argsort(axis=1)
    idx = np.take_along_axis(idx, order, axis=1)
    return idx, np.take_along_axis(sims, idx, axis=1)


def percentile(values: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """0 = lowest of the calls, 1 = highest (everything is placed against the calls)."""
    ref = np.sort(reference)
    return np.searchsorted(ref, values, side="right") / max(1, len(ref))


# The nodes of the "Materiality" and "Techniques" lenses: each is the meaning of
# its guide words (bge-m3). Edit freely and run the script again.
MATERIALITY = [
    ("Paper and print", ["paper, printed matter, books, posters, printmaking, risograph, letterpress, publishing in print"]),
    ("Light and film", ["light, analog film, photography, darkroom, projection, camera, photographic image"]),
    ("Space and body", ["space, the body, scenography, theatre stage, installation, exhibition space, architecture, performance"]),
    ("Matter and hand", ["handmade, craft, textile, ceramics, wood, sculpture, natural materials, making by hand"]),
    ("Screen and code", ["screen, software, code, digital media, interactive, web, apps, virtual reality, games"]),
    ("Data and maps", ["data, maps, cartography, statistics, information, archives, datasets, geographic information"]),
]
TECHNIQUES = [
    ("Printmaking", ["printmaking, etching, engraving, lithography, screen printing, letterpress, risograph"]),
    ("Photography", ["photography, photographers, photobook, documentary photography, camera, lens-based work"]),
    ("Set and exhibition design", ["set design, scenography, exhibition design, staging, spatial design, museum installation"]),
    ("Editorial design", ["editorial design, graphic design, typography, layout, books, magazines, visual identity"]),
    ("Data visualisation", ["data visualization, information design, infographics, charts, mapping data"]),
    ("Creative coding", ["creative coding, interactive installation, generative art, programming, physical computing"]),
    ("AI", ["artificial intelligence, machine learning, generative AI, algorithms, neural networks"]),
    ("Video and moving image", ["video, film, moving image, animation, documentary film, cinema"]),
    ("Research and investigation", ["research, investigation, open source investigation, forensic, journalism, academic study"]),
]
LENS_COLORS = ["#E8734A", "#F4C95D", "#5CC8C2", "#7FB069", "#6EA8FE", "#A78BFA", "#F28FB1", "#C49A6C", "#8C9AA6"]


def guide_nodes(model: str) -> list:
    """The materiality and technique nodes as vectors (asks Ollama to embed the guide words)."""
    lenses = [("material", "Materiality", MATERIALITY), ("technique", "Techniques", TECHNIQUES)]
    texts = [w for _, _, lnodes in lenses for _, words in lnodes for w in words]
    try:
        v = np.array(llm.embed(texts, model=model))
    except Exception as e:  # Ollama off: only the website categories lens
        print(f"Materiality and techniques skipped: {e}")
        return []
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    out, k = [], 0
    for lid, label, lnodes in lenses:
        vecs = []
        for _, words in lnodes:
            m = v[k:k + len(words)].mean(axis=0)
            vecs.append(m / np.linalg.norm(m))
            k += len(words)
        nodes_out = [{"id": name.lower().replace(" ", "-"), "label": name, "color": LENS_COLORS[j % len(LENS_COLORS)]}
                     for j, (name, _) in enumerate(lnodes)]
        out.append((lid, label, nodes_out, np.array(vecs)))
    return out


def guide_axis(model: str):
    """Directions of the analog and digital ends, from the guide words (asks Ollama)."""
    try:
        v = np.array(llm.embed(ANALOG + DIGITAL, model=model))
    except Exception as e:  # Ollama off: the map works without this sort
        print(f"Analog/digital sort skipped: {e}")
        return None
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    a, d = v[:len(ANALOG)].mean(axis=0), v[len(ANALOG):].mean(axis=0)
    return a / np.linalg.norm(a), d / np.linalg.norm(d)


# ---------------------------------------------------------------- build

def site_structure(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """The website's categories (name and colour) and projects, read through
    the portfolio loader every time (never copied into this app)."""
    from vs import portfolio as portfolio_mod
    angles = [n for n in nodes if n["g"] == "project"]
    try:
        p = portfolio_mod.load()
        cats = [{"id": c.id, "label": c.label, "color": c.color} for c in p.categories]
        projects = [{"slug": pr.slug, "label": pr.name, "year": pr.year, "cats": list(pr.categories)} for pr in p.projects]
    except Exception as e:  # the site can't be read: fall back to what the stored write-ups say
        print(f"Portfolio not readable ({e}); using the stored write-ups")
        cats = [{"id": c, "label": c.replace("-", " ").capitalize(), "color": ""} for c in sorted({a["cat"] for a in angles})]
        slugs = sorted({a["slug"] for a in angles})
        projects = [{"slug": s, "label": s.replace("-", " ").title(), "year": None,
                     "cats": sorted({a["cat"] for a in angles if a["slug"] == s})} for s in slugs]
    used = {c for pr in projects for c in pr["cats"]}
    cats = [c for c in cats if c["id"] in used]
    names = {pr["slug"]: pr["label"] for pr in projects}
    for a in angles:  # the project's real name, from the site
        a["label"] = names.get(a["slug"], a["label"])
    return cats, projects


def build() -> dict:
    nodes, x, model = collect()
    if not nodes:
        sys.exit("No embeddings in the database yet: run a refresh first.")
    cats, projects = site_structure(nodes)
    g = np.array([n["g"] for n in nodes])
    proj, opp = np.flatnonzero(g == "project"), np.flatnonzero(g == "opp")
    sims = x @ x.T
    np.fill_diagonal(sims, -1)
    # the project write-ups are the centre of the map, not dots: neighbours are calls, examples, studio projects
    nb_sims = sims.copy()
    nb_sims[:, proj] = -1
    idx, sv = top(nb_sims, K_LIST)

    # the closest project write-up (by name and category) and the closest thing you liked
    p_sims = sims[:, proj] if len(proj) else np.zeros((len(nodes), 1))
    p_best = proj[p_sims.argmax(axis=1)] if len(proj) else np.zeros(len(nodes), int)
    resemblance = p_sims.max(axis=1)
    liked = np.flatnonzero((g == "liked") | np.array([n.get("fav", False) for n in nodes]))
    disliked = np.flatnonzero((g == "disliked") | np.array([n.get("dis", False) for n in nodes]))
    l_sims = sims[:, liked] if len(liked) else np.zeros((len(nodes), 1))
    l_best = liked[l_sims.argmax(axis=1)] if len(liked) else np.full(len(nodes), -1)
    like = np.sort(l_sims, axis=1)[:, -3:].mean(axis=1)
    dislike = sims[:, disliked].max(axis=1) if len(disliked) else np.zeros(len(nodes))
    taste = like - 0.5 * np.clip(dislike, 0, None)
    axis = guide_axis(model)
    tech = (x @ axis[0] - x @ axis[1]) if axis is not None else np.zeros(len(nodes))
    ranks = {k: percentile(v, v[opp]) for k, v in {"proj": resemblance, "taste": taste, "tech": tech}.items()}

    # ---- the lenses: what the nodes between your projects and the calls are
    slug_index = {pr["slug"]: k for k, pr in enumerate(projects)}
    others = np.flatnonzero(g != "project")
    lenses = []

    def lens(lid: str, label: str, lnodes: list[dict], text_sim: np.ndarray, project_links: list[list]) -> None:
        """Each text hangs from the node it is most about; its strength (0..1, ranked
        among the calls of that node) sets how close to the node it sits."""
        node_of = text_sim.argmax(axis=1)
        best = text_sim.max(axis=1)
        strength, order = np.zeros(len(nodes)), np.zeros(len(nodes))
        for ni_ in range(len(lnodes)):
            members = others[node_of[others] == ni_]
            calls = members[g[members] == "opp"]
            ref = best[calls] if len(calls) else best[members]
            if len(members):
                strength[members] = percentile(best[members], ref)
            if len(members) > 2:  # alike texts side by side along the node's petal
                c = x[members] - x[members].mean(axis=0)
                _, _, vt = np.linalg.svd(c, full_matrices=False)
                v = c @ vt[0]
                order[members] = v.argsort().argsort() / (len(members) - 1)
            elif len(members):
                order[members] = np.linspace(0.3, 0.7, len(members))
            lnodes[ni_]["n"] = int(sum(1 for i in members if nodes[i]["g"] == "opp" and not nodes[i]["closed"]))
        lenses.append({"id": lid, "label": label, "nodes": lnodes, "plinks": project_links,
                       "of": node_of, "strength": strength, "order": order, "sim": best})

    # 1. the website's categories: a text joins the category whose project write-ups it reads most like
    cat_sim = np.full((len(nodes), len(cats)), -1.0)
    for ci, c in enumerate(cats):
        cols = [i for i in proj if nodes[i]["cat"] == c["id"]]
        if cols:
            cat_sim[:, ci] = sims[:, cols].max(axis=1)
    cat_index = {c["id"]: ci for ci, c in enumerate(cats)}
    lens("site", "Website categories", cats, cat_sim,
         [[k, cat_index[cid], 1.0] for k, pr in enumerate(projects) for cid in pr["cats"] if cid in cat_index])

    # 2-3. materiality and techniques: nodes made of guide words, measured with bge-m3
    guides = guide_nodes(model)
    if guides:
        # a project's text = the mean of its write-ups; it links to its two closest nodes
        pvec = np.zeros((len(projects), x.shape[1]))
        for i in proj:
            k = slug_index.get(nodes[i]["slug"])
            if k is not None:
                pvec[k] += x[i]
        pvec /= np.maximum(np.linalg.norm(pvec, axis=1, keepdims=True), 1e-9)
        for lid, label, lnodes, vecs in guides:
            psim = pvec @ vecs.T
            plinks = []
            for k in range(len(projects)):
                if not pvec[k].any():
                    continue
                for j in np.argsort(-psim[k])[:2]:
                    plinks.append([k, int(j), round(float(psim[k, j] / psim[k].max()), 3)])
            lens(lid, label, lnodes, x @ vecs.T, plinks)

    # output: the calls, examples and studio projects (the write-ups become the site's projects)
    keep = [i for i in range(len(nodes)) if g[i] != "project"]
    new = {old: k for k, old in enumerate(keep)}
    out = []
    for i in keep:
        n = nodes[i]
        n["nb"] = [[new[int(j)], round(float(s), 3)] for j, s in zip(idx[i], sv[i]) if int(j) in new]
        n["m"] = {k: round(float(v[i]), 4) for k, v in ranks.items()}
        best = nodes[p_best[i]] if len(proj) else None
        n["proj"] = [slug_index.get(best["slug"], -1), best["cat"], round(float(resemblance[i]), 3)] if best else None
        n["like"] = ([new[int(l_best[i])], round(float(l_sims[i].max()), 3)]
                     if len(liked) and l_best[i] != i and int(l_best[i]) in new else None)
        n["tech"] = round(float(tech[i]), 4)
        # per lens: [node, strength (1 = most about it), order along the node, similarity]
        n["L"] = [[int(L["of"][i]), round(float(L["strength"][i]), 4), round(float(L["order"][i]), 4),
                   round(float(L["sim"][i]), 3)] for L in lenses]
        out.append(n)
    for L in lenses:
        for k in ("of", "strength", "order", "sim"):
            del L[k]
    groups = [{"id": gid, "label": label, "n": int(sum(1 for n in out if n.get("grp") == gid and not n["closed"]))}
              for gid, label, _ in present.FEED_GROUPS]
    return {"nodes": out, "lenses": lenses, "projects": projects,
            "groups": [gr for gr in groups if gr["n"] or any(n.get("grp") == gr["id"] for n in out)],
            "model": model, "tech": axis is not None, "types": present.TYPE_LABELS, "board": DATA_DIR.name}


def main() -> None:
    from vs.web.icons import icon
    from vs.web.oppo import oppo
    data = build()
    # Oppo's own look: its stylesheet, its Lucide icons and its drawing (never a new one)
    data["icons"] = {n: str(icon(n, 16)) for n in sorted(set(ICONS) | set(present.TYPE_ICONS.values()))}
    data["type_icons"] = present.TYPE_ICONS
    data["oppo"] = str(oppo("default", 28))
    data["oppo_empty"] = str(oppo("empty", 96))
    app_css = (Path(__file__).resolve().parent.parent / "vs" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    html = Path(__file__).with_name("embedding_map.html").read_text(encoding="utf-8").replace("/*APPCSS*/", app_css)
    out = OUTPUT_DIR / ("embedding-map.html" if DATA_DIR.name == "data" else f"embedding-map-{DATA_DIR.name}.html")
    out.write_text(html.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
    nodes = data["nodes"]
    counts = {g: sum(n["g"] == g for n in nodes) for g in ("opp", "project", "liked", "disliked", "ref")}
    print(f"{len(nodes)} dots {counts}\n-> {out}")
    for L in data["lenses"]:
        print(f"  {L['label']}: " + ", ".join(f"{nd['label']} {nd['n']}" for nd in L["nodes"]))


if __name__ == "__main__":
    main()
