"""Title rules, on real cases from 1 Oct 2026 (self-contained configuration)."""

from vs import rules

FIELDS = (r"(?<![a-z])(design|graphic|grafic|visual|communicat|multimedia|media|photo|foto|video|data|information|"
          r"infograph|gis\b|geograph|carto|map|exhibition|editorial|publication|digital|content|creativ|art\b|cultur|"
          r"journalis|research|participat|communit|citizen|human rights|open call|ux\b|ui\b|illustra|animat)")
PREFS = {
    "intake": {"enabled": True, "field_titles": FIELDS},
    "taste": {"hide_if": ["human resources", "accounting", "procurement", "administrative assistant"]},
    "sources": {"un-careers": {"title_filter": True}, "who": {"title_filter": True}, "fao": {"title_filter": True},
                "uiuxjobsboard": {}, "fontanel": {}},
}


def _reason(title, source="un-careers", prefs=PREFS):
    return rules.reason(title, "", source, rules.compile_rules(prefs))


def test_rules_are_off_unless_enabled():
    off = {**PREFS, "intake": {**PREFS["intake"], "enabled": False}}
    assert not _reason("Technical Officer (Immunization)", "who", off)


def test_off_target_institutional_titles_are_hidden():
    assert _reason("Technical Officer (Immunization)", "who")
    assert _reason("Heating and Energy Efficiency Engineer", "fao")
    assert _reason("Human Resources Assistant", "fao").startswith("never-show")


def test_your_fields_are_kept():
    for t in ["Intern (Graphic Design)", "Infographic/Design, Photography/Video Editing Intern",
              "Intern - Geographic Information Systems (GIS)", "Intern - Human Rights",
              "Master Holder Fellowship for Citizen Science Project Manager",
              "Postdoc Position in Radical Communities at Aix-Marseille", "Perform EU Open Call 2026-2028",
              "Political Affairs Intern: Information analysis and data visualisation"]:
        assert not _reason(t), t


def test_design_wins_over_never_show():
    assert not _reason("Lead CX/UX Designer - Tax & Accounting Netherlands", "uiuxjobsboard")


def test_title_filter_only_on_marked_sources():
    assert not _reason("Programme Management Officer", "fontanel"), "only marked sources get the field filter"
    assert _reason("Programme Management Officer", "un-careers")
