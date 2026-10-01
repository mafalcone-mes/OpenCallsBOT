from datetime import date, timedelta

from vs import extract, present
from vs.examples import decayed, list_examples, log_signal, read_signals, save_example, taste_signals
from vs.fetch.polite import canonical_url
from vs.score import eligibility_check, keyword_score


def test_canonical_url_strips_tracking():
    a = canonical_url("https://www.Example.org/call/?utm_source=x&id=3#top")
    b = canonical_url("https://example.org/call?id=3")
    assert a == b


def test_verify_deadline_and_money():
    text = "Scadenza: 15 novembre 2026. Borsa di 1.200 euro al mese. Quota di iscrizione 35 €."
    data = {"deadline": "2026-11-15", "deadline_evidence": "Scadenza: 15 novembre 2026",
            "amount": 1200, "application_fee": 35, "money_kind": ["stipend", "fee_required"],
            "money_evidence": "Borsa di 1.200 euro al mese"}
    checks = extract.verify(data, text, "it")
    assert checks["deadline"]["verified"]
    assert checks["amount"]["verified"]
    assert checks["application_fee"]["verified"]


def test_verify_flags_invented_values():
    text = "Open call for designers. Deadline 1 December 2026."
    data = {"deadline": "2026-12-10", "deadline_evidence": "Deadline 10 December 2026",
            "amount": 5000, "money_kind": ["prize"], "money_evidence": "a prize of 5000 euros"}
    checks = extract.verify(data, text, "en")
    assert not checks["deadline"]["verified"]
    assert not checks["amount"]["verified"]
    assert not checks["money_kind"]["verified"]


def test_invented_nationality_limit_is_unverified():
    text = "Candidates from Sweden and other countries are invited to apply. Good command of English."
    data = {"nationalities": ["SE", "EU"], "eligibility_evidence": "Candidates from Sweden and other countries are invited to apply."}
    assert not extract.verify(data, text, "en")["eligibility"]["verified"]
    text2 = "Only Italian citizens under 35 may apply."
    data2 = {"nationalities": ["IT"], "age_max": 35, "eligibility_evidence": "Only Italian citizens under 35 may apply."}
    assert extract.verify(data2, text2, "en")["eligibility"]["verified"]


def test_money_badge_never_hides_fee():
    badge = present.money_badge({"money_kind": ["covers_costs", "fee_required"], "covers": ["housing"],
                                 "application_fee": 25, "currency": "EUR"})
    assert "covers housing" in badge and "fee €25" in badge


def test_eligibility_flags_but_needs_facts():
    me = {"birth_year": date.today().year - 30, "citizenships": ["IT"], "languages": {"it": "native", "en": "fluent"}}
    o = {"eligibility": {"age_max": 35, "nationalities": ["NL"]}, "application_languages": ["nl"], "conditions": {},
         "field_confidence": {"eligibility": {"verified": True}}}
    flag, notes = eligibility_check(o, me)
    assert flag == "likely_ineligible" and any("nationality" in n for n in notes)
    o["field_confidence"]["eligibility"]["verified"] = False
    flag, notes = eligibility_check(o, me)
    assert flag == "check", "a limit not confirmed on the page must never mark you ineligible"
    flag, _ = eligibility_check({"eligibility": {"age_max": 35}, "conditions": {}}, {})
    assert flag == "check"


def test_keywords_multilingual():
    score, hits = keyword_score("Residenza artistica sulla visualizzazione dati partecipativa",
                                {"keywords_plus": ["visualizzazione dati", "residenza artistica"], "keywords_minus": []})
    assert score > 0 and len(hits) == 2


def test_decay_halves_at_half_life():
    ts = (date.today() - timedelta(days=180)).isoformat() + "T00:00:00+00:00"
    assert abs(decayed(1.0, 180, ts) - 0.5) < 0.02
    assert decayed(1.0, 180, ts, pinned=True) == 1.0


def test_examples_are_files_and_signals_log():
    ex = save_example("interested", "An open call for participatory data installations.", url="https://x.org/call",
                      why="participatory angle")
    assert ex.path.exists() and "participatory angle" in ex.path.read_text(encoding="utf-8")
    assert any(e.id == ex.id for e in list_examples())
    log_signal("favourite", key="https://x.org/other")
    log_signal("dismiss", key="https://x.org/bad", reason="timing")
    events = read_signals()
    assert [e["kind"] for e in events][-2:] == ["favourite", "dismiss"]
    refs = {s["ref"]: s for s in taste_signals()}
    assert f"example:{ex.id}" in refs and refs[f"example:{ex.id}"]["w"] > 0
    assert "opp:https://x.org/other" in refs
    assert "opp:https://x.org/bad" not in refs, "a timing dismissal is not a taste signal"


def test_join_spelled_titles():
    from vs.references import _join_spelled

    raw = "Archivio\n   Prototipi  \n\n  M \n o\n\n d \nu\nl\ni\nNel laboratorio Moduli Ribelli"
    assert _join_spelled(raw) == "Archivio\nPrototipi\nModuli\nNel laboratorio Moduli Ribelli"
