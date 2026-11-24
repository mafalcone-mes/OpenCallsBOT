"""Sources for one field (sheet `tags`, e.g. photography) follow the portfolio's categories."""

from pathlib import Path

from vs import sheet
from vs.portfolio import Angle, Category, Portfolio
from vs.score import rerank_prompt


def _portfolio(cats: list[tuple[str, str]]) -> Portfolio:
    angles = [Angle(f"p{i}", cid, f"Project {i}", "", "", 0, "") for i, (cid, _) in enumerate(cats)]
    return Portfolio(root=Path("."), person={"name": "X", "role": "Y"}, categories=[Category(c, l, "#000") for c, l in cats],
                     projects=[], angles=angles, has_cv=False, cv_text="", css_tokens={}, profile_hash="h",
                     media_hash="m", warnings=[])


def test_tags_match_categories_by_id_label_or_prefix():
    p = _portfolio([("photography", "Photography"), ("graphic-design", "Graphic Design")])
    assert [c.id for c in p.categories_for(["photography"])] == ["photography"]
    assert [c.id for c in p.categories_for(["Photo"])] == ["photography"]
    assert [c.id for c in p.categories_for(["graphic design"])] == ["graphic-design"]
    assert p.categories_for(["architecture"]) == []
    assert p.categories_for([]) == []


def test_designer_without_photography_has_no_match():
    p = _portfolio([("ux-ui-design", "UX/UI Design")])
    assert p.categories_for(["photography"]) == []


def test_sheet_tags_become_source_fields(monkeypatch):
    rows = [{"kind": "source", "id": "fpo", "name": "FPO", "enabled": "TRUE", "url": "https://x/feed",
             "how_read": "rss", "tags": "photography, photo"}]
    monkeypatch.setattr(sheet, "rows", lambda cfg: (rows, None))
    monkeypatch.delenv("VS_NO_SHEET", raising=False)
    data = sheet.apply_to_prefs({"sheet": {"enabled": True, "url": "x"}, "sources": {}, "watchlist": []})
    assert data["sources"]["fpo"]["field_tags"] == ["photography", "photo"]


def test_prompt_says_to_judge_within_the_field():
    p = _portfolio([("photography", "Photography")])
    r = {"best_angles": ["p0:photography"], "near_pos": [], "near_neg": [], "fields": ["Photography"]}
    user, _ = rerank_prompt(p, {"title_en": "Open call"}, r, [], {})
    assert "SOURCE FOR PHOTOGRAPHY" in user
    user, _ = rerank_prompt(p, {"title_en": "Open call"}, {**r, "fields": []}, [], {})
    assert "SOURCE FOR" not in user
