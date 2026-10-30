"""Structured extraction of one opportunity page with the local LLM, then a
deterministic check of every claim against the source text."""

from __future__ import annotations

import re
from datetime import date

import dateparser
from dateparser.search import search_dates
from rapidfuzz import fuzz

from vs import llm

TYPES = ["job", "residency", "open_call", "grant", "commission", "competition", "workshop",
         "summer_school", "fellowship", "phd", "research_position", "other"]
MONEY_KINDS = ["salary", "stipend", "grant", "fee_paid_to_you", "prize", "covers_costs", "free_no_support",
               "fee_required", "unknown"]
COVERS = ["travel", "housing", "studio", "materials", "production", "per_diem", "meals", "visa"]
DOC_KINDS = ["cv", "portfolio", "motivation_letter", "project_proposal", "budget", "references",
             "work_samples", "video", "id_document", "diploma", "writing_sample", "other"]

_str = {"type": "string"}
# Hard caps on free text: without them the model can start copying the whole
# page into a quote and run until the context is full (minutes per page).
_quote = {"type": "string", "maxLength": 250}
_short = {"type": "string", "maxLength": 200}
_num_or_null = {"type": ["number", "null"]}

EXTRACT_VERSION = "2"  # bump when SYSTEM changes: rejected items are read again

SCHEMA = {
    "type": "object",
    "properties": {
        "is_opportunity": {"type": "boolean"},
        "type": {"type": "string", "enum": TYPES},
        "title_en": _short,
        "org_name": _short,
        "summary_en": {"type": "string", "maxLength": 700},
        "field_tag": _short,
        "location_city": _str,
        "location_country": _str,
        "remote_mode": {"type": "string", "enum": ["onsite", "hybrid", "remote", "online", "unknown"]},
        "deadline": _str,
        "deadline_kind": {"type": "string", "enum": ["fixed", "rolling", "unknown"]},
        "deadline_evidence": _quote,
        "start_date": _str,
        "duration_text": _str,
        "money_kind": {"type": "array", "items": {"type": "string", "enum": MONEY_KINDS}},
        "amount": _num_or_null,
        "currency": _str,
        "amount_period": _str,
        "covers": {"type": "array", "items": {"type": "string", "enum": COVERS}},
        "application_fee": _num_or_null,
        "money_evidence": _quote,
        "age_min": {"type": ["integer", "null"]},
        "age_max": {"type": ["integer", "null"]},
        "nationalities": {"type": "array", "items": _str},
        "residence": {"type": "array", "items": _str},
        "education": _str,
        "career_stage": _str,
        "eligibility_evidence": _quote,
        "application_languages": {"type": "array", "items": _str},
        "required_docs": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": DOC_KINDS}, "details": _short, "required": {"type": "boolean"}},
            "required": ["kind", "details", "required"]}},
        "submission_method": {"type": "string", "enum": ["email", "form", "platform", "post", "unknown"]},
        "submission_url": _str,
        "anonymous_submission": {"type": "boolean"},
        "seniority": {"type": "string", "enum": ["student", "entry", "junior", "mid", "senior", "any", "unknown"]},
        "other_dates": {"type": "array", "items": {"type": "object", "properties": {"label": _str, "date": _str},
                                                   "required": ["label", "date"]}},
    },
    "required": ["is_opportunity", "type", "title_en", "org_name", "summary_en", "field_tag", "location_city",
                 "location_country", "remote_mode", "deadline", "deadline_kind", "deadline_evidence", "start_date",
                 "duration_text", "money_kind", "amount", "currency", "amount_period", "covers", "application_fee",
                 "money_evidence", "age_min", "age_max", "nationalities", "residence", "education", "career_stage",
                 "eligibility_evidence", "application_languages", "required_docs", "submission_method",
                 "submission_url", "anonymous_submission", "seniority", "other_dates"],
}

SYSTEM = """You extract facts from calls for artists, designers and researchers (jobs, residencies, open calls, grants, fellowships, PhDs, workshops).
The page may be in English, Italian, Dutch, German, Spanish or another language. Write title_en, summary_en and field_tag in English.
Rules:
- Only state what the text says. If something is not in the text, use "" / null / [] / "unknown". Never guess.
- *_evidence fields: copy the exact short phrase (at most 20 words) from the ORIGINAL text, in the original language, that contains the value. "" if none.
- nationalities / residence / age limits: ONLY when the text explicitly restricts who may apply. "Candidates from X and other countries are invited" is NOT a restriction: use [].
- is_opportunity: true for any call one can apply to: job, residency, open call, grant, fellowship, prize, workshop with registration,
  even if most of the text is background, and even if its deadline has passed (that is handled separately).
  If the listing title announces an open call, grant, fellowship or residency, treat it as an opportunity unless the text clearly
  says it is only a report about past winners or an event. false for news articles, interviews, exhibition reviews and event announcements.
- deadline: application deadline as YYYY-MM-DD. start_date likewise.
- location_country: ISO 3166-1 alpha-2 code (e.g. IT, NL, DE). nationalities/residence: ISO codes or "EU".
- money_kind: every condition that applies. "salary" = employment. "stipend" = regular allowance (residency, fellowship, PhD). "grant" = project funding. "fee_paid_to_you" = artist fee or commission. "prize" = award money. "fee_required" = the applicant must pay (application or participation fee). "covers_costs" = travel/housing/materials paid but no fee to the applicant. "free_no_support" = nothing paid either way.
- amount: the main money figure for the applicant (salary, stipend, prize or grant), as a number. application_fee: fee the applicant pays, as a number.
- application_languages: ISO 639-1 codes of the languages applications may be written in.
- summary_en: 2 to 3 plain sentences: who it is for, what is offered, what is asked.
- required_docs details: at most 12 words each (format, limits).
- field_tag: 2 to 5 words naming the field (e.g. "exhibition design", "investigative journalism", "biophysics")."""


# Two steps: a short answer first (is this a call at all?), the rest only for
# calls. Writing the answer is most of the time per page, and both steps send
# the same messages so Ollama reuses the page it has already read.
FIRST_FIELDS = ["is_opportunity", "type", "title_en", "org_name", "deadline", "deadline_kind", "deadline_evidence"]


def _part(fields: list[str]) -> dict:
    return {"type": "object", "properties": {k: SCHEMA["properties"][k] for k in fields}, "required": fields}


FIRST_SCHEMA = _part(FIRST_FIELDS)
REST_SCHEMA = _part([k for k in SCHEMA["required"] if k not in FIRST_FIELDS])
MAX_TEXT = 12000


def extract(text: str, *, title: str = "", url: str = "", lang: str = "", today: date | None = None) -> dict:
    today = today or date.today()
    user = (f"Today is {today.isoformat()}.\nURL: {url}\nListing title: {title}\nDetected language: {lang or 'unknown'}\n\n"
            f"--- PAGE TEXT ---\n{text[:MAX_TEXT]}")
    model = llm.extract_model()
    data = llm.chat_json(SYSTEM, user, FIRST_SCHEMA, model=model, max_tokens=1000, purpose="extract: is it a call")
    if not data.get("is_opportunity", True):
        return data
    data.update(llm.chat_json(SYSTEM, user, REST_SCHEMA, model=model, max_tokens=2000, purpose="extract: details"))
    return data


# ---------------------------------------------------------------- checks

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    q, t = _norm(quote), _norm(text)
    if not q:
        return False
    if q in t:
        return True
    return fuzz.partial_ratio(q, t) >= 90 if len(q) < 600 else False


def source_window(quote: str, text: str) -> str:
    """The stretch of the SOURCE text that the quote matches ('' if none), so
    facts are read from the page, not from the model's copy of it."""
    q, t = _norm(quote), re.sub(r"\s+", " ", text or "").strip()
    if not q:
        return ""
    i = t.lower().find(q)
    if i >= 0:
        return t[i:i + len(q)]
    if len(q) >= 600:
        return ""
    al = fuzz.partial_ratio_alignment(q, t.lower())
    return t[al.dest_start:al.dest_end] if al and al.score >= 90 else ""


def _numbers_in(text: str) -> set[float]:
    found = set()
    for m in re.finditer(r"\d[\d.,' ]*\d|\d", text):
        raw = m.group(0).replace(" ", "").replace("'", "")
        # 1.200,50 / 1,200.50 / 1200
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+([.,]\d{1,2})?", raw):
            # thousands separators; a trailing 1-2 digit group is decimals
            digits = re.sub(r"[.,](\d{1,2})$", r"#\1", raw)
            digits = digits.replace(".", "").replace(",", "").replace("#", ".")
        else:
            digits = raw.replace(",", ".")
        try:
            found.add(float(digits))
        except ValueError:
            continue
    return found


def _dates_in(snippet: str, languages: list[str] | None) -> set[str]:
    """Every date dateparser can find in a snippet (it may hold several)."""
    if not snippet:
        return set()
    settings = {"PREFER_DATES_FROM": "future", "REQUIRE_PARTS": ["day", "month"]}
    found = set()
    for langs in (languages, None):  # retry without the language hint
        try:
            hits = search_dates(snippet, languages=langs, settings=settings) or []
        except Exception:
            hits = []
        found |= {d.date().isoformat() for _, d in hits}
        if found:
            break
    for m in re.finditer(r"\d{4}-\d{2}-\d{2}", snippet):
        found.add(m.group(0))
    # Fallback: explicit dates read one by one, when surrounding words
    # ("before", "(23:59 CET)") confuse the search.
    explicit = re.compile(r"\b\d{1,2}(?:st|nd|rd|th|\.)?\s+[A-Za-zÀ-ÿ]{3,12}\.?,?\s+\d{4}\b"
                          r"|\b[A-Za-zÀ-ÿ]{3,12}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
                          r"|\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")
    for m in explicit.finditer(snippet):
        for langs in (languages, None):
            try:
                d = dateparser.parse(m.group(0), languages=langs, settings={"DATE_ORDER": "DMY"})
            except Exception:
                d = None
            if d:
                found.add(d.date().isoformat())
                break
    return found


def verify(data: dict, text: str, lang: str = "") -> dict:
    """Returns {field: {"verified": bool, "evidence": str, "why": str}} for the
    fields that matter for decisions, and normalises dates in `data`."""
    checks: dict[str, dict] = {}
    languages = [lang] if lang else None

    # Deadline: must be in the text (evidence) and parse to the same day.
    dl = (data.get("deadline") or "").strip()
    ev = data.get("deadline_evidence") or ""
    if dl:
        window = source_window(ev, text)
        ok_quote = bool(window)
        same = dl[:10] in _dates_in(window, languages)
        checks["deadline"] = {"verified": ok_quote and same, "evidence": ev,
                              "why": "" if ok_quote and same else ("quote not found in page" if not ok_quote else "date in quote differs")}
        try:
            date.fromisoformat(dl[:10])
        except ValueError:
            data["deadline"] = ""
            checks["deadline"] = {"verified": False, "evidence": ev, "why": f"not a date: {dl}"}

    # Money: amounts and fees must literally appear in the page.
    numbers = _numbers_in(text)
    for field in ("amount", "application_fee"):
        value = data.get(field)
        if value not in (None, 0):
            ok = any(abs(n - float(value)) < 0.01 for n in numbers)
            checks[field] = {"verified": ok, "evidence": data.get("money_evidence") or "",
                             "why": "" if ok else "number not found in page"}
    if data.get("money_kind") and data["money_kind"] != ["unknown"]:
        ev = data.get("money_evidence") or ""
        ok = quote_in_text(ev, text)
        checks["money_kind"] = {"verified": ok, "evidence": ev, "why": "" if ok else "no supporting quote"}

    # Eligibility limits: the quote must be on the page, an age limit must be
    # in it, and a nationality/residence limit needs restriction wording.
    data["residence"] = [r for r in data.get("residence") or [] if r and r.lower() != "unknown"]
    data["nationalities"] = [n for n in data.get("nationalities") or [] if n and n.lower() != "unknown"]
    if data.get("age_min") or data.get("age_max") or data.get("nationalities") or data.get("residence"):
        ev = data.get("eligibility_evidence") or ""
        window = source_window(ev, text)
        ok, why = bool(window), "" if window else "no supporting quote"
        if ok and data.get("age_max") and not re.search(rf"\b({data['age_max']}|{data['age_max'] + 1})\b", window):
            ok, why = False, "age limit not in quote"
        if ok and (data.get("nationalities") or data.get("residence")) and not RESTRICTION.search(window):
            ok, why = False, "no explicit nationality/residence restriction in quote"
        checks["eligibility"] = {"verified": ok, "evidence": ev, "why": why}
    return checks


RESTRICTION = re.compile(
    r"\bonly\b|must (be|have|hold|reside)|citizen|national(s|ity)\b|resident|residing|based in|living in|"
    r"cittadin|residen|nazionalit|domicil|nationaliteit|ingezetene|woonachtig|staatsangeh|wohnsitz|ansässig|"
    r"nacionalidad|residente|ciudadan|nationalité|résid", re.I)
