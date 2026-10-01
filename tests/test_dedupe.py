"""Cases taken from the real database on 1 Oct 2026."""

from vs.dedupe import why_same


def o(title, org="", deadline="", city=""):
    return {"title_en": title, "org_name": org, "deadline": deadline, "location_city": city}


def test_same_post_twice_is_merged():
    a = o("2023 Art Space Grant, a collaboration between KADIST and AFIELD", "AFIELD")
    b = o("2023 Art Space Grant, a collaboration between KADIST and AFIELD", "AFIELD", "")
    assert why_same(a, b)


def test_same_role_same_org_one_deadline_missing_is_merged():
    assert why_same(o("AI Creative Specialist", "Magnet Studio", "2026-10-20"), o("AI Creative Specialist", "Magnet Studio"))


def test_same_call_on_two_sites_is_merged():
    a = o("Art Explora – Cité internationale des arts Residency programme 2027", "Art Explora Foundation", "2026-11-04")
    b = o("Art Explora / Cité internationale des arts: Residency 2027 (France)", "Cité internationale des arts & Art Explora", "2026-11-04")
    assert why_same(a, b, cos=0.95)


def test_un_job_on_reliefweb_and_un_careers_is_merged():
    a = o("Intern - Information Management", "UN Office for the Coordination of Humanitarian Affairs (OCHA)", "2026-10-06", "Istanbul")
    b = o("Intern - Information Management", "United Nations Secretariat, Office for the Coordination of Humanitarian Affairs",
          "2026-10-07", "Istanbul")
    assert why_same(a, b, cos=0.96), "a one-day deadline gap is a time-zone difference"


def test_title_with_org_and_city_added_is_the_same_role():
    a = o("Creative Internship at Haines", "Haines", "2026-10-26", "London")
    b = o("Creative Internship at Haines in London", "Haines", "", "London")
    assert why_same(a, b, cos=0.93)


def test_two_who_positions_in_the_same_survey_stay_separate():
    a = o("National Consultant: Data Manager for STEPS Survey on NCD Risk Factors", "World Health Organization (WHO)", city="Bujumbura")
    b = o("National Consultant Coordinator for STEPS Survey on Non-Communicable Diseases", "World Health Organization (WHO) Burundi",
          city="Bujumbura")
    assert not why_same(a, b, cos=0.95)


def test_same_programme_different_year_stays_separate():
    a = o("MSCA Doctoral Networks 2026", "European Commission (Horizon Europe)", "2026-11-24")
    b = o("MSCA Doctoral Networks 2027", "European Commission (Horizon Europe)", "2027-11-23")
    assert not why_same(a, b, cos=0.98)


def test_fao_regional_internships_stay_separate():
    a = o("Internship Programme for Europe and Central Asia (REU)", "Food and Agriculture Organization (FAO)")
    b = o("Internship Programme for Asia and the Pacific (RAP)", "Food and Agriculture Organization (FAO)")
    assert not why_same(a, b, cos=0.93), "similar text without an exact anchor must not merge"


def test_same_title_different_companies_stay_separate():
    assert not why_same(o("UI/UX Designer", "Acme"), o("UI/UX Designer", "Globex"), cos=0.97)


def test_longer_title_is_a_different_role():
    assert not why_same(o("UI/UX Designer", "Wolters Kluwer"), o("Senior UX/UI Designer (Maternity Cover)", "Wolters Kluwer"))


def test_same_role_different_city_stays_separate():
    assert not why_same(o("Graphic Designer", "Studio X", city="Milan"), o("Graphic Designer", "Studio X", city="Amsterdam"))


def test_different_deadlines_stay_separate():
    a = o("PhD specialisation in Communication - EMANAIRE", "BI Norwegian Business School", "2026-11-01")
    b = o("PhD specialisation in Communication - EMANAIRE", "BI Norwegian Business School", "2026-10-01")
    assert not why_same(a, b, cos=0.99)


def test_different_programmes_same_deadline_stay_separate():
    a = o("MSCA Choose Europe for Science 2027 Postdoctoral", "European Commission", "2027-04-06")
    b = o("MSCA COFUND 2027 Doctoral Programme", "European Commission", "2027-04-06")
    assert not why_same(a, b, cos=0.88)
