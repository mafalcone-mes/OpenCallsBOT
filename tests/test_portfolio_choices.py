"""You edit Oppo's portfolio proposal (projects, order, importance), say why,
and the next proposal is built with your reasons in view."""

from pathlib import Path

from fastapi.testclient import TestClient

from vs import brief, choices, db
from vs import portfolio as portfolio_mod
from vs.portfolio import Angle, Category, Portfolio, Project


def _pf() -> Portfolio:
    cats = [Category("photography", "Photography", "#000"), Category("graphic-design", "Graphic Design", "#111")]
    angles = [Angle("a", "photography", "Alpha", "", "", 10, ""), Angle("a", "graphic-design", "Alpha", "", "", 4, ""),
              Angle("b", "photography", "Beta", "", "", 6, ""), Angle("c", "graphic-design", "Gamma", "", "", 5, ""),
              Angle("d", "photography", "Delta", "", "", 2, "")]
    projects = [Project(s, n, 2024, []) for s, n in [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma"), ("d", "Delta")]]
    return Portfolio(root=Path("."), person={"name": "X", "role": "Y"}, categories=cats, projects=projects, angles=angles,
                     has_cv=False, cv_text="", css_tokens={}, profile_hash="h", media_hash="m", warnings=[])


PROPOSAL = [{"angle": "a:photography", "importance": "main"}, {"angle": "b:photography", "importance": "supporting"},
            {"angle": "c:graphic-design", "importance": "brief"}]
FINAL = [{"angle": "b:photography", "importance": "main"}, {"angle": "a:graphic-design", "importance": "supporting"},
         {"angle": "d:photography", "importance": "brief"}]


def test_diff_names_every_kind_of_change():
    kinds = {(c["kind"], c["angle"].split(":")[0]) for c in choices.diff(PROPOSAL, FINAL)}
    assert kinds == {("importance", "b"), ("category", "a"), ("importance", "a"), ("added", "d"), ("removed", "c"),
                     ("moved", "b"), ("moved", "a")}
    assert choices.diff(PROPOSAL, PROPOSAL) == []
    texts = [choices.describe(c, _pf()) for c in choices.diff(PROPOSAL, FINAL)]
    assert "Moved Beta up (from 2 to 1)" in texts and "Removed Gamma" in texts
    assert "Showed Alpha as Graphic Design instead of Photography" in texts


def test_importance_sets_the_images_and_a_page_limit_shrinks_the_least_important_first():
    pf = _pf()
    sel = [dict(s) for s in [{"angle": "a:photography", "importance": "main"}, {"angle": "b:photography", "importance": "supporting"},
                             {"angle": "c:graphic-design", "importance": "brief"}]]
    assert brief._image_plan(pf, sel, {}, True, True) == ["a:photography:8", "b:photography:3", "c:graphic-design:1"]
    # 10 pages - cover - about - 3 openings = 5 spare -> room for 7 images
    plan = brief._image_plan(pf, [dict(s) for s in sel], {"pages": 10}, True, True)
    images = [int(p.rsplit(":", 1)[1]) for p in plan]
    assert sum(images) <= 7 and images[0] >= images[1] >= images[2] >= 1


def test_the_editor_lists_the_proposal_first_then_every_other_project():
    rows = brief.editor_rows(_pf(), [{"angle": "b:photography", "importance": "main", "why": "fits"}],
                             ["c:graphic-design", "a:photography"])
    assert [r["slug"] for r in rows] == ["b", "c", "a", "d"]
    assert rows[0]["shown"] and not rows[1]["shown"] and rows[0]["why"] == "fits"
    assert [k for k, _, _ in rows[2]["categories"]] == ["a:photography", "a:graphic-design"]


def test_the_next_proposal_sees_your_rules_and_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(choices, "CHOICES_PATH", tmp_path / "c.jsonl")
    monkeypatch.setattr(choices, "RULES_PATH", tmp_path / "r.json")
    changes = choices.diff(PROPOSAL, FINAL)
    for c in changes:
        c["why"] = "documentary work leads for photo calls" if c["kind"] == "moved" and c["angle"].startswith("b") else ""
    rec = {"call_key": "k1", "title": "Photo prize", "org": "Gallery", "call_text": "photo prize", "proposal": PROPOSAL,
           "final": FINAL, "changes": changes, "note": "keep it short"}
    choices.save(rec)
    choices.set_rules(["Lead with documentary work", ""])
    assert choices.load() == [rec] and choices.rules() == ["Lead with documentary work"]
    monkeypatch.setattr(choices, "similar", lambda conn, o, model, k=3, min_sim=0.5: choices.load())
    text = choices.prompt_block(None, {"dedup_key": "k2"}, _pf(), "m")
    assert "- Lead with documentary work" in text
    assert "Beta as Photography (Main)" in text and "because documentary work leads" in text and "keep it short" in text


def test_confirm_then_notes_are_saved_and_the_pdf_is_built_from_your_choice(tmp_path, monkeypatch):
    from vs.web import app as web
    monkeypatch.setattr(choices, "CHOICES_PATH", tmp_path / "c.jsonl")
    monkeypatch.setattr(choices, "RULES_PATH", tmp_path / "r.json")
    monkeypatch.setattr(portfolio_mod, "load", lambda *a, **k: _pf())
    built, distilled = {}, []
    monkeypatch.setattr(brief, "build", lambda oid, sel, **k: built.update(oid=oid, sel=sel) or {})
    monkeypatch.setattr(choices, "distill", lambda pf: distilled.append(1) or [])
    with db.session() as conn:
        oid = conn.execute("INSERT INTO opportunity(dedup_key, canonical_url, title_en, status, extraction_status, first_seen) "
                           "VALUES('pc1','https://x/pc1','Photo prize','open','done',?)", (db.now(),)).lastrowid
        db.kv_set(conn, f"brief-draft:{oid}", {"selected": [{**s, "why": "model says"} for s in PROPOSAL],
                                               "ranked": [], "at": db.now()})
    client = TestClient(web.app)
    page = client.get(f"/o/{oid}")
    assert "Make the portfolio PDF" in page.text and "Delta" in page.text  # every project is offered
    form = {"order": "b,a,d,c", "show": ["b", "a", "d"], "angle_b": "b:photography", "imp_b": "main",
            "angle_a": "a:graphic-design", "imp_a": "supporting", "angle_d": "d:photography", "imp_d": "brief",
            "angle_c": "c:graphic-design", "imp_c": "brief"}
    r = client.post(f"/o/{oid}/brief/confirm", data=form, follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(50):
        if built:
            break
        time.sleep(0.02)
    assert [s["angle"] for s in built["sel"]] == ["b:photography", "a:graphic-design", "d:photography"]
    assert built["sel"][0]["why"] == "model says"  # the model's reason is kept where you kept its pick
    page = client.get(f"/o/{oid}")
    assert "Why did you change" in page.text and "Removed Gamma" in page.text
    with db.session() as conn:
        fb = db.kv_get(conn, f"brief-feedback:{oid}")
    removed = next(c for c in fb["changes"] if c["kind"] == "removed")
    client.post(f"/o/{oid}/brief/feedback", data={f"why_{removed['id']}": "too commercial", "note": ""})
    saved = choices.load()
    assert saved[0]["call_key"] == "pc1"
    assert next(c for c in saved[0]["changes"] if c["kind"] == "removed")["why"] == "too commercial"
    for _ in range(50):
        if distilled:
            break
        time.sleep(0.02)
    assert distilled  # your reason updates the rules
    with db.session() as conn:
        assert db.kv_get(conn, f"brief-feedback:{oid}") is None and db.kv_get(conn, f"brief-draft:{oid}") is None
