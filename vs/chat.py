"""The home page chat. The model only turns a question into a search plan
(JSON, temperature 0, fixed seed, cached per question and day); the app runs
the plan on its own data and answers with what it found. No sentence of an
answer is written by the model, so the same question on the same data always
gives the same answer, and nothing in it can be made up.

What it can search: the opportunities and your state (saved, applications,
dismissed), the sources and studios, your portfolio projects. What it can do,
after you confirm: save, unsave, dismiss or set the status of results it
showed, or start a refresh."""

from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from vs import db, embed, llm, present, report
from vs.config import prefs

PLAN_VERSION = 4
INTENTS = ["search", "near_project", "sources", "studios", "projects", "action", "talk", "help"]
HISTORY = 4  # earlier turns of the same chat the model sees
ACTIONS = ["", "save", "unsave", "dismiss", "set_status", "refresh"]
TARGETED = {"save", "unsave", "dismiss", "set_status"}
SHOW = 10  # results shown per answer

EMPTY_PLAN = {"intent": "help", "words": [], "topic": "", "types": [], "region": "", "country": "", "within_days": 0,
              "deadline_before": "", "money": "", "no_fee": False, "remote": "", "status": "", "project": "",
              "action": "", "targets": [], "reason": "", "app_status": ""}


def schema(projects: list[str]) -> dict:
    s = {"type": "object", "properties": {
        "intent": {"enum": INTENTS},
        "words": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 5},
        "topic": {"type": "string", "maxLength": 120},
        "types": {"type": "array", "items": {"enum": list(present.TYPE_LABELS)}, "maxItems": 5},
        "region": {"enum": ["", "IT", "EU", "world"]},
        "country": {"type": "string", "maxLength": 2},
        "within_days": {"type": "integer", "minimum": 0, "maximum": 366},
        "deadline_before": {"type": "string", "maxLength": 10},
        "money": {"enum": [""] + list(present.MONEY_LABELS)},
        "no_fee": {"type": "boolean"},
        "remote": {"enum": ["", "yes", "no"]},
        "status": {"enum": ["", "favourite", "tracked", "dismissed"]},
        "project": {"enum": [""] + projects},
        "action": {"enum": ACTIONS},
        "targets": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 50}, "maxItems": 20},
        "reason": {"enum": [""] + [k for k, _ in present.DISMISS_REASONS]},
        "app_status": {"enum": [""] + present.STATUSES},
    }}
    s["required"] = ["intent"]  # the rest only when asked: unasked fields stay out
    return s


def system_prompt(today: date, projects: list[tuple[str, str]]) -> str:
    end_of_month = today.replace(day=monthrange(today.year, today.month)[1])
    names = "; ".join(f"{slug} = {name}" for slug, name in projects) or "none"
    return f"""You turn one question to an opportunities app into a search plan. Only answer with the JSON.
Today is {today.isoformat()}. The question may be in Italian or English.

intent:
- search: find opportunities (jobs, residencies, open calls, grants, fellowships, workshops, courses, PhDs, competitions).
- near_project: opportunities close to one of the person's own portfolio projects (set project).
- sources: questions about where the app looks (sources, websites, watched pages).
- studios: design studios to approach.
- projects: the person's own portfolio projects.
- action: they ask to save, unsave, dismiss or change the application status of results already shown
  ("the first two" -> targets [1, 2]; "all" -> every number shown), or to start a new search of the sources (action refresh).
- talk: a question about results already shown, not a new search ("why the second one?", "which fits me best?",
  "tell me more about 3", "compare 1 and 2"), or plain conversation ("thanks", "what should I do first?").
- help: greetings, questions about what the app can do.

Earlier questions of the same chat may come first. When the new question is a follow-up ("and in Germany?",
"only the paid ones", "e quelli online?"), the plan keeps what the earlier question asked and changes only what
the new one changes. When it is a new subject, ignore the earlier questions.

Fields (leave empty: "", [], 0, false when not asked):
- words: exact words or names they want to find in the text, only when they name a specific word ("with the word X", "che contengono X", a place, an organisation).
- topic: the subject in a few English words, for meaning-based ranking ("landscape and participatory art").
- types: lavoro/job/stage/internship -> job; residenza -> residency; bando/open call/call -> open_call; borsa/grant -> grant;
  fellowship -> fellowship; workshop/corso/course -> workshop; summer school -> summer_school; dottorato/PhD -> phd;
  concorso/premio/competition/prize -> competition; commissione/commission -> commission; ricerca/research position -> research_position.
- region: Italia/Italy -> IT; Europa/Europe -> EU; outside Europe -> world. country: a two-letter code only for one other country (NL, DE, FR...).
- within_days: "next two weeks" -> 14. deadline_before: an end date, "this month" -> {end_of_month.isoformat()}, "entro dicembre" -> last day of December.
- money: paid/stipendiato -> salary; stipend; grant; fee_paid_to_you; prize; covers_costs. no_fee: "senza quota", "free to apply".
- remote: online/remote -> yes; in presence -> no.
- status: salvati/saved/favourites -> favourite; candidature/applications -> tracked; scartati/dismissed -> dismissed.
- project: one of: {names}.
- reason (dismiss): one of {", ".join(k for k, _ in present.DISMISS_REASONS)}. app_status: one of {", ".join(present.STATUSES)}."""


def normalise(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower()).rstrip(" ?!.")


def _project_list(portfolio) -> list[tuple[str, str]]:
    return [(p.slug, p.name) for p in getattr(portfolio, "projects", [])] if portfolio else []


def plan_for(conn, question: str, portfolio, today: date | None = None, n_shown: int = 0,
             history: list[str] | tuple = ()) -> tuple[dict, str]:
    """(plan, note). The plan is cached per question, earlier questions, day and
    model, so asking again gives exactly the same plan even if the model would
    now say otherwise. `history`: the chat's earlier questions, oldest first."""
    today = today or date.today()
    q = normalise(question)
    if not q:
        return dict(EMPTY_PLAN), ""
    acting = ground(dict(EMPTY_PLAN), question, n_shown)
    if acting["intent"] == "action":  # "save 1 and 3": read from the words, no model needed
        return acting, ""
    projects = _project_list(portfolio)
    try:
        model = llm.chat_model()
    except llm.LLMError as e:
        return ground(_fallback(q), question), f"The local model isn't available ({e}): I searched for the words instead."
    history = [h for h in history if normalise(h)][-HISTORY:]
    context = " ".join(history[-2:])  # a follow-up may keep what the last questions asked
    key = "chatplan:" + hashlib.sha256(
        f"{PLAN_VERSION}|{model}|{today}|{q}|{[normalise(h) for h in history]}|{[s for s, _ in projects]}".encode()).hexdigest()[:24]
    cached = db.kv_get(conn, key)
    if cached:
        return {**EMPTY_PLAN, **cached}, ""
    try:
        user = question.strip()
        if history:
            user = "Earlier questions in this chat, oldest first:\n" + "\n".join(f"- {h.strip()}" for h in history) + f"\n\nNew question: {user}"
        plan = llm.chat_json(system_prompt(today, projects), user, schema([s for s, _ in projects]),
                             model=model, temperature=0.0, seed=7, max_tokens=400, purpose="chat")
    except llm.LLMError as e:
        return ground(_fallback(q), question), f"The local model didn't answer ({e}): I searched for the words instead."
    plan = ground(clean({**EMPTY_PLAN, **plan}, today), question, context=context)
    db.kv_set(conn, key, plan)
    return plan, ""


def _fallback(q: str) -> dict:
    words = [w for w in re.findall(r"[\w'-]+", q) if len(w) > 2][:5]
    return {**EMPTY_PLAN, "intent": "search", "words": words}


# Every filter in a plan must be grounded in the question: a field stays only
# if the question has a word that asks for it (the model tends to fill in
# fields nobody asked for). Matching is on lowercase substrings, Italian and English.
TYPE_CUES = {
    "job": ["lavor", "job", "impieg", "posizion", "position", "stage", "tirocin", "intern", "vacanc", "vacatur", "assunz", "hiring", "career"],
    "residency": ["residen"], "open_call": ["open call", "call", "bando", "bandi", "avvis"],
    "grant": ["grant", "borsa", "borse", "finanzia", "funding", "contribut"], "fellowship": ["fellow"],
    "workshop": ["workshop", "corso", "corsi", "course", "laborator", "formazion", "training", "class"],
    "summer_school": ["summer school", "scuola estiva", "scuole estive"], "phd": ["phd", "dottorat", "doctor"],
    "research_position": ["research", "ricerca", "postdoc", "assegn"],
    "competition": ["concors", "premio", "premi ", "prize", "award", "competition", "contest"],
    "commission": ["commission", "incaric"], "other": [],
}
REGION_CUES = {"IT": ["itali", "ital"], "EU": ["europ"], "world": ["mondo", "world", "fuori dall", "outside", "estero", "abroad"]}
COUNTRY_CUES = {
    "NL": ["olanda", "paesi bassi", "netherlands", "holland", "dutch", "olandes", "amsterdam", "rotterdam", "eindhoven", "utrecht", "aia", "hague"],
    "DE": ["germania", "germany", "german", "tedesc", "berlin", "monaco di baviera", "munich", "amburgo", "hamburg", "colonia", "cologne", "lipsia", "leipzig"],
    "FR": ["francia", "france", "frances", "french", "parigi", "paris", "lione", "lyon", "marsiglia"],
    "ES": ["spagna", "spain", "spagnol", "spanish", "barcellona", "barcelona", "madrid"],
    "BE": ["belgio", "belgium", "belg", "bruxelles", "brussels", "anversa", "antwerp", "gent", "ghent"],
    "GB": ["regno unito", "united kingdom", "inghilterra", "england", "britain", "londra", "london", " uk"],
    "CH": ["svizzera", "switzerland", "swiss", "zurigo", "zurich", "ginevra", "geneva", "basilea", "basel", "losanna", "lausanne"],
    "AT": ["austria", "vienna", "wien", "linz"], "PT": ["portogallo", "portugal", "lisbona", "lisbon", "porto"],
    "US": ["stati uniti", "united states", "usa", "america", "new york"], "SE": ["svezia", "sweden", "stoccolma", "stockholm"],
    "DK": ["danimarca", "denmark", "copenaghen", "copenhagen"], "NO": ["norvegia", "norway", "oslo"], "FI": ["finlandia", "finland", "helsinki"],
    "PL": ["polonia", "poland", "varsavia", "warsaw"], "CZ": ["repubblica ceca", "czech", "praga", "prague"], "GR": ["grecia", "greece", "atene", "athens"],
}
COUNTRY_CODES = {"GB": ["GB", "UK"]}  # the data writes the United Kingdom both ways
MONEY_CUES = {"salary": ["pagat", "retribu", "salar", "paid"], "stipend": ["stipend"], "grant": ["grant", "borsa"], "prize": ["premio", "prize"],
              "fee_paid_to_you": ["compenso", "fee paid", "pagat", "paid"], "covers_costs": ["spese", "costs", "coper", "covered"]}
STATUS_CUES = {"favourite": ["salvat", "saved", "preferit", "favourit", "stellin", "starred"], "tracked": ["candidatur", "application", "applied", "candidato"],
               "dismissed": ["scartat", "dismissed", "nascost", "hidden"]}
NO_FEE_CUES = ["senza quota", "gratuit", "free to apply", "no fee", "senza costi", "without a fee", "non a pagamento"]
REMOTE_CUES = {"yes": ["online", "remot", "a distanza", "da casa"], "no": ["in presenza", "on site", "in sede", "on-site"]}
WITHIN_CUES = ["settiman", "week", "giorn", "days", "mese", "month", "presto", "soon", "prossim", "next"]
BEFORE_CUES = ["entro", "by ", "before", "prima d", "questo mese", "this month", "fine ", "end of", "scad", "clos", "chiud"]
SOURCE_CUES = ["fonti", "fonte", "sources", "source", "siti", "sito", "websites", "website", "pagine sorvegliate", "watched"]
STUDIO_CUES = ["studi ", "studi?", "studios", "studio di design", "design studio", "agenzi", "agenc"]


def _has(q: str, cues) -> bool:
    return any(c in q for c in cues)


# Italian city names, written as the data writes them (English).
CITY_NAMES = {"berlino": "Berlin", "monaco di baviera": "Munich", "parigi": "Paris", "londra": "London", "barcellona": "Barcelona",
              "bruxelles": "Brussels", "l'aia": "The Hague", "zurigo": "Zurich", "ginevra": "Geneva", "lisbona": "Lisbon",
              "anversa": "Antwerp", "colonia": "Cologne", "lipsia": "Leipzig", "amburgo": "Hamburg", "praga": "Prague", "varsavia": "Warsaw",
              "milano": "Milan", "torino": "Turin", "firenze": "Florence", "venezia": "Venice", "napoli": "Naples", "roma": "Rome", "genova": "Genoa"}
# Kinds named without doubt (no "ricerca": "fai una ricerca" means "search").
SURE_TYPES = ["job", "residency", "open_call", "grant", "fellowship", "workshop", "summer_school", "phd", "competition"]
ORDINALS = {"prim": 1, "first": 1, "second": 2, "terz": 3, "third": 3, "quart": 4, "fourth": 4, "quint": 5, "fifth": 5,
            "sest": 6, "sixth": 6, "settim": 7, "seventh": 7, "ottav": 8, "eighth": 8, "non": 9, "ninth": 9, "decim": 10, "tenth": 10}
ACTION_CUES = [("unsave", r"\b(togli|rimuovi) dai salvati\b|\bunsave\b|\bremove from saved\b"),
               ("save", r"\bsalva(la|le|li|lo)?\b|\bsave\b|\bmetti tra i salvati\b"),
               ("dismiss", r"\bscarta(la|le|li|lo)?\b|\bnascondi\b|\bdismiss\b|\bhide\b|\bnon mi interessa"),
               ("set_status", r"\b(segna|imposta|metti)\b.*\b(come|a|in)\b|\bset\b.*\bto\b|\bstato\b"),
               ("refresh", r"\b(cerca di nuovo|aggiorna|rifai la ricerca|search (my sources )?again|refresh)\b")]
STATUS_WORDS = {"shortlisted": ["shortlist", "da valutare"], "preparing": ["preparing", "preparazione", "sto preparando", "preparo"],
                "submitted": ["submitted", "inviat", "mandat"], "selected": ["selected", "selezionat", "vinto"],
                "rejected": ["rejected", "rifiutat", "scartato da"], "no_answer": ["no answer", "nessuna risposta", "senza risposta"]}
REASON_WORDS = {"not_my_field": ["not my field", "non è il mio campo", "non c'entra", "fuori tema"], "not_my_taste": ["taste", "gusto", "non mi piace"],
                "too_corporate": ["corporate", "aziendale"], "wrong_seniority": ["senior", "junior", "livello"], "timing": ["timing", "tempo", "tempi"],
                "location": ["location", "luogo", "lontan", "troppo distante"], "money": ["money", "soldi", "paga", "compenso"],
                "ineligible": ["eligib", "idone", "requisit"]}


def _numbers(q: str, n_shown: int) -> list[int]:
    """'1 e 3', 'the first two', 'tutti' -> result numbers."""
    if re.search(r"\b(tutti|tutte|all|everything)\b", q):
        return list(range(1, n_shown + 1))
    m = re.search(r"\b(prim[ei]|first)\s+(due|tre|quattro|cinque|two|three|four|five)\b", q)
    if m:
        k = {"due": 2, "tre": 3, "quattro": 4, "cinque": 5, "two": 2, "three": 3, "four": 4, "five": 5}[m.group(2)]
        return list(range(1, k + 1))
    nums = [int(x) for x in re.findall(r"\b(\d{1,2})\b", q)]
    nums += [n for stem, n in ORDINALS.items() if re.search(rf"\b{stem}\w*\b", q) and stem != "non"]
    return sorted(set(nums))


def ground(plan: dict, question: str, n_shown: int = 0, context: str = "") -> dict:
    """Keep only what the question asks for (see the cue lists above). In a
    follow-up, `context` (the earlier questions) may keep a field the model
    carried over; what the app adds on its own comes from the question only."""
    q = " " + normalise(question) + " "
    asked = q  # the new question alone
    q = q + normalise(context) + " " if context else q
    # Actions are read from the words themselves: "salva la 1 e la 2" always means the same.
    for act, pattern in ACTION_CUES:
        if re.search(pattern, q) and (act == "refresh" or _numbers(q, n_shown) or plan.get("intent") == "action"):
            plan.update(intent="action", action=act)
            if act != "refresh":
                plan["targets"] = _numbers(q, n_shown) or plan.get("targets") or []
            plan["app_status"] = next((s for s, ws in STATUS_WORDS.items() if _has(q, ws)), plan.get("app_status") or "")
            plan["reason"] = next((r for r, ws in REASON_WORDS.items() if _has(q, ws)), plan.get("reason") or "")
            return plan
    plan["types"] = [t for t in plan.get("types") or [] if _has(q, TYPE_CUES.get(t, []))]
    if not plan["types"] and plan.get("intent") in ("search", "near_project"):
        plan["types"] = [t for t in SURE_TYPES if _has(asked, TYPE_CUES[t])]
    if plan.get("region") and not _has(q, REGION_CUES.get(plan["region"], [])):
        plan["region"] = ""
    if plan.get("country") and not _has(q, COUNTRY_CUES.get(plan["country"], [plan["country"].lower()])):
        plan["country"] = ""
    if plan.get("country") and plan.get("region") == "world" and plan["country"] not in ("US",):
        plan["region"] = ""
    if plan.get("money") and not _has(q, MONEY_CUES.get(plan["money"], [])):
        plan["money"] = ""
    plan["no_fee"] = bool(plan.get("no_fee")) and _has(q, NO_FEE_CUES)
    if plan.get("remote") and not _has(q, REMOTE_CUES.get(plan["remote"], [])):
        plan["remote"] = ""
    if plan.get("within_days") and not (re.search(r"\d", q) or _has(q, WITHIN_CUES)):
        plan["within_days"] = 0
    if plan.get("deadline_before") and not _has(q, BEFORE_CUES):
        plan["deadline_before"] = ""
    if plan.get("deadline_before") and plan.get("within_days") and not re.search(r"\d", q):
        plan["within_days"] = 0  # "this month": one deadline, not two
    # Your own lists are named by a word in the question, whatever the model said.
    plan["status"] = next((s for s, cues in STATUS_CUES.items() if _has(asked, cues)), "") or \
        (plan.get("status") if plan.get("status") in STATUS_CUES and _has(q, STATUS_CUES[plan["status"]]) else "")
    # Words to find in the text must be in the question, and not be the kind,
    # place or list already filtered on ("residenze", "salvati").
    filtered = [c for cues in list(TYPE_CUES.values()) + list(STATUS_CUES.values()) + list(REGION_CUES.values()) for c in cues]
    words = []
    for w in plan.get("words") or []:
        lw = w.lower()
        if lw in CITY_NAMES and lw in q:
            words.append(CITY_NAMES[lw])  # "Berlino" is found as "Berlin"
        elif lw in q and not any(c.strip() and lw == c.strip() for c in filtered) \
                and not any(lw.startswith(c.strip()) and len(c.strip()) > 4 for c in filtered if c.strip() not in ("designer",)) \
                and not any(c.strip() and c.strip() in lw for cues in COUNTRY_CUES.values() for c in cues if c.strip() not in CITY_NAMES.values()):
            words.append(w)
    # a city named in the question is a word to find, even if the model only set the country
    for it, en in CITY_NAMES.items():
        if re.search(rf"\b{re.escape(it)}\b", asked) and en not in words:
            words.append(en)
    plan["words"] = words[:5]
    if plan.get("intent") in ("search", "help") and _has(asked, SOURCE_CUES):
        plan["intent"] = "sources"
    elif plan.get("intent") in ("search", "help") and _has(asked, STUDIO_CUES):
        plan["intent"] = "studios"
    if plan.get("intent") == "search" and plan.get("project"):
        plan["intent"] = "near_project"
    return plan


def clean(plan: dict, today: date) -> dict:
    """Drop values the app can't use (a malformed date, an empty word)."""
    plan["words"] = [w.strip() for w in plan.get("words") or [] if w and w.strip()][:5]
    plan["topic"] = (plan.get("topic") or "").strip()
    if plan.get("deadline_before"):
        try:
            if date.fromisoformat(plan["deadline_before"]) < today:
                plan["deadline_before"] = ""
        except ValueError:
            plan["deadline_before"] = ""
    plan["country"] = (plan.get("country") or "").upper()[:2]
    if plan.get("intent") == "action" and not plan.get("action"):
        plan["intent"] = "help"
    return plan


# ---------------------------------------------------------------- answers

@dataclass
class Answer:
    kind: str                       # opportunities | sources | studios | projects | confirm | help
    title: str
    chips: list[str] = field(default_factory=list)
    rows: list = field(default_factory=list)
    note: str = ""
    feed_link: str = ""
    loosen: list = field(default_factory=list)   # (label, n, plan) when nothing was found
    confirm: dict | None = None
    check: list = field(default_factory=list)   # sites the question names, to read now when nothing was found
    plan: dict = field(default_factory=dict)


def describe(plan: dict, portfolio=None) -> list[str]:
    """The plan in words, shown above every answer, so you see what was searched."""
    chips = [present.TYPE_LABELS.get(t, t) for t in plan.get("types") or []]
    chips += [{"IT": "Italy", "EU": "Europe", "world": "Outside Europe"}[plan["region"]]] if plan.get("region") else []
    chips += [f"in {plan['country']}"] if plan.get("country") else []
    chips += [f"“{w}”" for w in plan.get("words") or []]
    chips += [f"about: {plan['topic']}"] if plan.get("topic") else []
    if plan.get("deadline_before"):
        chips.append(f"closes by {date.fromisoformat(plan['deadline_before']).strftime('%d %b %Y')}")
    chips += [f"closes within {plan['within_days']} days"] if plan.get("within_days") else []
    chips += [present.MONEY_LABELS.get(plan["money"], plan["money"])] if plan.get("money") else []
    chips += ["no fee"] if plan.get("no_fee") else []
    chips += [{"yes": "remote / online", "no": "on site"}[plan["remote"]]] if plan.get("remote") else []
    chips += [{"favourite": "saved", "tracked": "your applications", "dismissed": "dismissed"}[plan["status"]]] if plan.get("status") else []
    if plan.get("project"):
        name = dict(_project_list(portfolio)).get(plan["project"], plan["project"])
        chips.append(f"close to {name}")
    return chips


def _opportunity_sql(plan: dict) -> tuple[str, list]:
    f = {k: v for k, v in {"region": plan.get("region"), "money": plan.get("money"),
                           "nofee": "1" if plan.get("no_fee") else "", "remote": plan.get("remote"),
                           "status": plan.get("status")}.items() if v}
    where, params = report.filter_sql(f)
    if plan.get("within_days"):
        # closes in the window, or (courses, events) starts in it
        where += (" AND ((o.deadline != '' AND o.deadline BETWEEN date('now') AND date('now', ?))"
                  " OR (COALESCE(o.deadline, '') = '' AND COALESCE(o.start_date, '') BETWEEN date('now') AND date('now', ?)))")
        params += [f"+{int(plan['within_days'])} day"] * 2
    if plan.get("types"):
        where += f" AND o.type IN ({','.join('?' * len(plan['types']))})"
        params += list(plan["types"])
    if plan.get("country"):
        codes = COUNTRY_CODES.get(plan["country"], [plan["country"]])
        where += f" AND upper(COALESCE(o.location_country, '')) IN ({','.join('?' * len(codes))})"
        params += codes
    if plan.get("deadline_before"):
        where += " AND o.deadline != '' AND o.deadline <= ?"
        params.append(plan["deadline_before"])
    for w in plan.get("words") or []:
        where += (" AND (o.title_en LIKE ? OR o.title_original LIKE ? OR o.org_name LIKE ? OR o.summary_en LIKE ?"
                  " OR o.raw_text LIKE ? OR o.location_city LIKE ?)")
        params += [f"%{w}%"] * 6
    return where + " AND COALESCE(m.bucket, 'feed') IN ('feed', 'discovery')", params


def _rank(conn, rows: list[dict], vecs_query: list[np.ndarray]) -> list[dict]:
    """Order rows by closeness to the query vectors (a topic, or a project's
    angles); the feed order breaks ties, so the result is always the same."""
    model = llm.embed_model()
    keys = [r["dedup_key"] for r in rows]
    have = {}
    for k in keys:
        row = conn.execute("SELECT vec FROM embedding WHERE kind='opportunity' AND key=? AND model_id=?", (k, model)).fetchone()
        if row:
            have[k] = np.frombuffer(row["vec"], dtype=np.float32)
    q = np.vstack(vecs_query)
    def closeness(r):
        v = have.get(r["dedup_key"])
        return float((q @ v).max()) if v is not None else -1.0
    order = sorted(range(len(rows)), key=lambda i: (-round(closeness(rows[i]), 4), i))
    return [rows[i] for i in order]


def _feed_link(plan: dict) -> str:
    """The same search in the feed, when the feed's filters can express it."""
    from urllib.parse import urlencode
    if plan.get("country") or plan.get("deadline_before") or plan.get("topic") or plan.get("project") or len(plan.get("words") or []) > 1:
        return ""
    groups = {g for g, _, types in present.FEED_GROUPS for t in plan.get("types") or [] if t in types}
    if len(groups) > 1:
        return ""
    q = {k: v for k, v in {"g": next(iter(groups), ""), "region": plan.get("region"), "within": plan.get("within_days") or "",
                           "money": plan.get("money"), "nofee": "1" if plan.get("no_fee") else "", "remote": plan.get("remote"),
                           "status": plan.get("status"), "q": (plan.get("words") or [""])[0]}.items() if v}
    return "/feed?" + urlencode(q)


def search(conn, plan: dict, portfolio=None) -> Answer:
    where, params = _opportunity_sql(plan)
    rows = report.feed_rows(conn, where, tuple(params), 300)
    note = ""
    if rows and (plan.get("topic") or plan.get("project")):
        try:
            if plan.get("project"):
                angles = embed.angle_vectors(conn, portfolio, llm.embed_model())
                qv = [v for k, v in angles.items() if k.split(":")[0] == plan["project"]]
            else:
                qv = list(embed.ensure(conn, "query", {plan["topic"]: plan["topic"]}, llm.embed_model()).values())
            if qv:
                rows = _rank(conn, rows, qv)
        except llm.LLMError:
            note = "The local model isn't available, so they are in feed order rather than by closeness."
    total = len(rows)
    a = Answer("opportunities", "", describe(plan, portfolio), rows[:SHOW], note, _feed_link(plan), plan=plan)
    if total:
        a.title = f"{total} found" + (f", the {SHOW} closest first" if total > SHOW and (plan.get("topic") or plan.get("project"))
                                      else f", the best {SHOW} first" if total > SHOW else "")
    else:
        a.title = "Nothing found"
        a.loosen = _loosen(conn, plan)
    return a


LOOSEN = [("words", "without the words", []), ("deadline_before", "with any deadline", ""), ("within_days", "with any deadline", 0),
          ("types", "of any kind", []), ("region", "anywhere", ""), ("country", "in any country", ""), ("money", "with any money", ""),
          ("no_fee", "with or without a fee", False), ("remote", "on site or remote", ""), ("status", "not only yours", "")]


def _loosen(conn, plan: dict) -> list[tuple[str, int, dict]]:
    out = []
    for k, label, empty in LOOSEN:
        if plan.get(k):
            p2 = {**plan, k: empty}
            if k == "deadline_before":
                p2["within_days"] = 0
            where, params = _opportunity_sql(p2)
            n = conn.execute(f"SELECT COUNT(*) {report.FEED_FROM} {where}", params).fetchone()[0]
            if n and all(lab != label for lab, _, _ in out):
                out.append((label, n, p2))
    return sorted(out, key=lambda x: -x[1])[:4]


GENERIC = {"sources", "source", "websites", "website", "sites", "pages", "studios", "studio", "design", "jobs", "opportunities",
           "projects", "project", "that", "with", "which", "look", "about", "list", "find", "show"}


def _stem(word: str) -> str:
    """A loose root, so "musei", "museo" and "museums" all find "museum"."""
    w = word.lower()
    return w[:max(4, len(w) - 2)] if len(w) > 4 else w


def _topic_words(plan: dict) -> list[str]:
    """Topic words that can match a name or tag: not the generic ones."""
    return [w for w in re.findall(r"[a-z]+", (plan.get("topic") or "").lower()) if len(w) > 3 and w not in GENERIC]


def sources(conn, plan: dict) -> Answer:
    p = prefs()
    items = [(sid, cfg) for sid, cfg in p.get("sources", {}).items() if cfg.get("enabled", True)]
    items += [(f"watch:{w['name']}", {"adapter": "watch", **w}) for w in p.get("watchlist", []) if w.get("enabled", True)]
    states = {r["source_id"]: db.row_dict(r) for r in conn.execute("SELECT source_id, last_ok FROM source_state")}
    counts = {r["source_id"]: r["c"] for r in conn.execute("SELECT source_id, COUNT(*) c FROM opportunity WHERE status='open' GROUP BY source_id")}
    needles = [_stem(w) for w in plan.get("words") or []] + [_stem(w) for w in _topic_words(plan)]
    def text(sid, cfg):
        return " ".join(str(x) for x in (sid, cfg.get("name", ""), cfg.get("url", ""), cfg.get("tags", ""), cfg.get("kind_hint", ""))).lower()
    if needles:
        items = [(sid, cfg) for sid, cfg in items if any(n in text(sid, cfg) for n in needles)]
    rows = [{"id": sid, "name": cfg.get("name") or sid.removeprefix("watch:"), "url": cfg.get("url", ""),
             "kind": "watched page" if cfg.get("adapter") == "watch" else cfg.get("adapter", ""),
             "last_ok": ((states.get(sid) or {}).get("last_ok") or "")[:10], "open": counts.get(sid, 0)} for sid, cfg in items]
    rows.sort(key=lambda r: (-r["open"], r["name"].lower()))
    title = f"{len(rows)} source{'s' if len(rows) != 1 else ''}" + (" match" if needles else " and watched pages in use")
    return Answer("sources", title, describe(plan), rows[:25], plan=plan)


def studios(plan: dict) -> Answer:
    from vs import studios as studios_mod
    items = studios_mod.load()
    if plan.get("country"):
        items = [s for s in items if (s.get("country") or "").upper() in COUNTRY_CODES.get(plan["country"], [plan["country"]])]
    elif plan.get("region") == "IT":
        items = [s for s in items if (s.get("country") or "").upper() == "IT"]
    needles = [_stem(w) for w in plan.get("words") or []] + [_stem(w) for w in _topic_words(plan)]
    if needles:
        items = [s for s in items if any(n in " ".join([s.get("name", ""), s.get("city", ""), " ".join(s.get("types", [])),
                                                         s.get("description", "")]).lower() for n in needles)]
    items.sort(key=lambda s: (s.get("country", ""), s.get("city", ""), s["name"]))
    return Answer("studios", f"{len(items)} studio{'s' if len(items) != 1 else ''}", describe(plan), items[:30], plan=plan)


def projects(plan: dict, portfolio) -> Answer:
    items = list(getattr(portfolio, "projects", []) or [])
    needles = [_stem(w) for w in plan.get("words") or []] + [_stem(w) for w in _topic_words(plan)]
    if needles:
        items = [p for p in items if any(n in f"{p.name} {p.slug} {' '.join(p.categories)}".lower() for n in needles)]
    return Answer("projects", f"{len(items)} of your projects", describe(plan), items, plan=plan)


def action(conn, plan: dict, last: list[int]) -> Answer:
    act = plan["action"]
    if act == "refresh":
        return Answer("confirm", "Search all your sources again?", [], note="It runs in the background; new results appear in the feed as they are scored.",
                      confirm={"action": "refresh"}, plan=plan)
    nums = sorted({n for n in plan.get("targets") or [] if 1 <= n <= len(last)})
    if not last:
        return Answer("help", "Which ones?", note="Ask a search first, then for example: “save the first two”.", plan=plan)
    if not nums:
        return Answer("help", "Which ones?", note=f"Say which of the {len(last)} results shown, e.g. “save 1 and 3”.", plan=plan)
    ids = [last[n - 1] for n in nums]
    rows = [db.row_dict(conn.execute("SELECT id, title_en, title_original FROM opportunity WHERE id=?", (i,)).fetchone()) for i in ids]
    rows = [r for r in rows if r]
    verb = {"save": "Save", "unsave": "Remove from saved", "dismiss": "Dismiss", "set_status": "Set the status of"}[act]
    extra = ""
    if act == "dismiss":
        extra = f" ({dict(present.DISMISS_REASONS).get(plan.get('reason') or 'other', 'Other')})"
    if act == "set_status":
        if not plan.get("app_status"):
            return Answer("help", "Which status?", note="One of: " + ", ".join(s.replace("_", " ") for s in present.STATUSES) + ".", plan=plan)
        extra = f" to “{plan['app_status'].replace('_', ' ')}”"
    title = f"{verb} {len(rows)} result{'s' if len(rows) != 1 else ''}{extra}?"
    return Answer("confirm", title, [f"#{n}" for n in nums], rows=rows,
                  confirm={"action": act, "ids": [r["id"] for r in rows], "reason": plan.get("reason") or "other",
                           "app_status": plan.get("app_status") or ""}, plan=plan)


def answer(conn, question: str, portfolio, last: list[int], plan: dict | None = None,
           history: list[str] | tuple = ()) -> tuple[Answer, str]:
    """Run one question (or a plan from a button) and return the answer."""
    note = ""
    if plan is None:
        plan, note = plan_for(conn, question, portfolio, n_shown=len(last), history=history)
    intent = plan.get("intent")
    # "Is Accurat looking for someone?", "has the fablab open calls?": a question
    # naming sites the app reads is about their openings, not about the source
    # itself (unless it says "source") nor a general search.
    named = named_sites(question) if question and (
        intent in ("search", "help", "studios") or (intent == "sources" and not _has(" " + normalise(question) + " ", SOURCE_CUES))) else []
    if named:
        a = from_sites(conn, plan, named)
    elif intent in ("search", "near_project"):
        a = search(conn, plan, portfolio)
    elif intent == "sources":
        a = sources(conn, plan)
    elif intent == "studios":
        a = studios(plan)
    elif intent == "projects":
        a = projects(plan, portfolio)
    elif intent == "action":
        a = action(conn, plan, last)
    elif intent == "talk":  # about what is already on screen: the reply is the answer
        rows = {r["id"]: r for r in report.feed_rows(conn, f"AND o.id IN ({','.join('?' * len(last))})", tuple(last), len(last))} if last else {}
        a = Answer("talk", "", rows=[rows[i] for i in last if i in rows], plan=plan)
    else:
        a = Answer("help", "What I can do", plan=plan)
    if a.kind == "opportunities" and not a.rows and question and not a.check:
        a.check = named_sites(question)
    if note:
        a.note = (a.note + " " + note).strip()
    return a, json.dumps(plan, ensure_ascii=False, sort_keys=True)


def do(payload: dict) -> str:
    """Carry out a confirmed action. Returns what was done, in words."""
    from vs.examples import log_signal
    act = payload.get("action")
    ids = [int(i) for i in payload.get("ids") or []]
    with db.session() as conn:
        keys = [r["dedup_key"] for r in (conn.execute("SELECT dedup_key FROM opportunity WHERE id=?", (i,)).fetchone() for i in ids) if r]
    for k in keys:
        if act == "save":
            log_signal("favourite", key=k)
        elif act == "unsave":
            log_signal("unfavourite", key=k)
        elif act == "dismiss":
            reason = payload.get("reason") if payload.get("reason") in dict(present.DISMISS_REASONS) else "other"
            log_signal("dismiss", key=k, reason=reason)
        elif act == "set_status" and payload.get("app_status") in present.STATUSES:
            log_signal("status", key=k, status=payload["app_status"])
    n = len(keys)
    return {"save": f"Saved {n}.", "unsave": f"Removed {n} from saved.", "dismiss": f"Dismissed {n}. The feed learns from it.",
            "set_status": f"Status set on {n}."}.get(act, "Done.")


# ---------------------------------------------------------------- the reply

REPLY_SYSTEM = """You are the assistant of an app that finds jobs, residencies, open calls and grants for one artist/designer.
The app has already answered the question with the FACTS below (its search results or its own data). Write a short reply
to the person, 2 to 4 sentences, like a helpful colleague: what was found, which result looks most promising and why,
or a direct answer to their follow-up question, and one useful next step or question to ask.

Rules:
- Use only the FACTS and the earlier conversation. Never invent calls, organisations, dates, places, money or requirements.
- Refer to results by their number, like #2. If the facts don't answer the question, say so plainly.
- Don't repeat the whole list: the person sees it under your reply.
- Write in the language of the person's last question (Italian or English). Plain text, no markdown, no lists."""


def _row_facts(n: int, r: dict, full: bool) -> str:
    parts = [f"#{n} {r.get('title_en') or r.get('title_original') or ''}"]
    for label, v in (("organisation", r.get("org_name")), ("kind", present.TYPE_LABELS.get(r.get("type") or "", r.get("type"))),
                     ("where", ", ".join(x for x in (r.get("location_city"), r.get("location_country")) if x)),
                     ("deadline", present.deadline_label(r.get("deadline") or "", r.get("deadline_kind") or "")),
                     ("money", present.money_badge(r.get("conditions"))),
                     ("how well it fits the person's work, by the app's score", f"{round(r['final'] * 100)}/100" if isinstance(r.get("final"), (int, float)) else ""),
                     ("summary", (r.get("summary_en") or "")[:700 if full else 220])):
        if v:
            parts.append(f"{label}: {v}")
    if full:
        for label, k in (("who can apply", "eligibility"), ("conditions", "conditions"), ("documents asked", "required_docs")):
            if r.get(k):
                parts.append(f"{label}: {json.dumps(r[k], ensure_ascii=False)[:500]}")
    return "; ".join(parts)


def facts(a: Answer) -> str:
    """What the app answered, in words the model can lean on."""
    lines = [a.title] if a.title else []
    if a.chips:
        lines.append("Searched for: " + ", ".join(a.chips))
    if a.note:
        lines.append(a.note)
    if a.kind in ("opportunities", "talk", "confirm"):
        lines += [_row_facts(i, r, a.kind == "talk") for i, r in enumerate(a.rows, 1)]
        if a.kind == "talk" and not a.rows:
            lines.append("No results are on screen in this chat yet.")
        if a.check:
            lines.append(("The results above are what the app saved from " if a.rows else "Nothing saved about ")
                         + ", ".join(s["name"] for s in a.check)
                         + ". Under your reply the person can ask the app to read their site(s) again now.")
        if a.loosen:
            lines.append("Nothing matched; with one filter less there would be: " + ", ".join(f"{l} ({n})" for l, n, _ in a.loosen))
    elif a.kind == "sources":
        lines += [f"{s['name']} ({s['kind']}, {s['open']} open)" for s in a.rows]
    elif a.kind == "studios":
        lines += [f"{s['name']}, {s.get('city', '')} {s.get('country', '')}: {', '.join(s.get('types') or [])}" for s in a.rows]
    elif a.kind == "projects":
        lines += [f"{p['name']} ({p.get('year', '')}): {', '.join(p.get('categories') or [])}" for p in a.rows]
    elif a.kind == "help":
        lines.append("The app can: find opportunities by kind, place, deadline, money or words; find what is close to a "
                     "portfolio project; show saved ones and applications; list sources and studios; save, dismiss or "
                     "set the status of results shown, after confirmation.")
    return "\n".join(lines)


def reply(question: str, history: list[tuple[str, str]], fact_text: str) -> str:
    """2-4 sentences on top of the app's answer; "" if the model is away."""
    convo = "".join(f"Person: {q}\nYou: {r}\n" for q, r in history[-HISTORY:])
    user = (f"EARLIER CONVERSATION\n{convo}\n" if convo else "") + f"FACTS\n{fact_text}\n\nPERSON'S QUESTION\n{question}"
    try:
        out = llm.chat_json(REPLY_SYSTEM, user, {"type": "object", "properties": {"reply": {"type": "string", "maxLength": 900}},
                                                 "required": ["reply"]},
                            temperature=0.2, seed=7, max_tokens=350, purpose="chat")
    except llm.LLMError:
        return ""
    return (out.get("reply") or "").strip()


# ---------------------------------------------------------------- named sites

# Words that don't tell one organisation from another ("Fondazione", "Studio",
# a city), so a name is matched on its own words ("Prada", "Accurat").
NAME_GENERIC = {
    "studio", "studios", "design", "museum", "museo", "musee", "foundation", "fondazione", "fondation", "fundacion", "stichting",
    "gallery", "galleria", "school", "scuola", "academy", "accademia", "akademie", "center", "centre", "centro", "zentrum",
    "open", "call", "calls", "jobs", "lavoro", "lavori", "careers", "the", "and", "for", "art", "arts", "arte", "kunst",
    "residency", "residenze", "residencies", "festival", "institute", "istituto", "institut", "university", "universita",
    "network", "project", "projects", "international", "european", "europe", "italia", "italy", "contemporary",
    "contemporanea", "contemporaneo", "cultural", "culture", "cultura", "media", "digital", "science", "research",
    "ricerca", "biennale", "kunsthalle", "society", "association", "associazione", "national", "nazionale", "city",
    "house", "casa", "park", "parco", "space", "spazio", "watch", "photo", "photography", "fotografia", "new", "grant",
    "grants", "programme", "program", "news", "feed", "bando", "bandi", "www", "https", "http",
}
NAME_GENERIC |= {c.strip() for cues in COUNTRY_CUES.values() for c in cues} | {k for k in CITY_NAMES} | {v.lower() for v in CITY_NAMES.values()}


def _words(text: str) -> list[str]:
    import unicodedata
    t = unicodedata.normalize("NFKD", text.lower())
    return re.findall(r"[a-z0-9]+", "".join(c for c in t if not unicodedata.combining(c)))


def _site_keys(*names: str) -> set[str]:
    """How a site can be named: its whole name run together ("fablabbarcelona"),
    its first two words ("fablab"), and its distinctive words ("accurat")."""
    keys = set()
    for name in names:
        w = _words(re.sub(r"\(.*?\)", " ", name or ""))  # "(workshops)" says what is read there, not who
        if not w:
            continue
        keys.add("".join(w))
        if len(w) > 1 and len("".join(w[:2])) >= 5:
            keys.add("".join(w[:2]))
        keys |= {x for x in w if len(x) >= 5 and x not in NAME_GENERIC and not x.isdigit()}
    return {k for k in keys if len(k) >= 4 and k not in NAME_GENERIC}


def from_sites(conn, plan: dict, sites: list[dict]) -> Answer:
    """What the app has from the named sites; you can have them read again now."""
    ids = [s["id"] for s in sites]
    rows = report.feed_rows(conn, f"AND o.source_id IN ({','.join('?' * len(ids))})", tuple(ids), 300)
    names = ", ".join(s["name"] for s in sites[:3]) + (f" and {len(sites) - 3} more" if len(sites) > 3 else "")
    a = Answer("opportunities", f"{len(rows)} open from {names}" if rows else f"Nothing saved from {names}",
               [f"from {names}"], rows[:SHOW], plan=plan, check=sites)
    if len(rows) > SHOW:
        a.title += f", the best {SHOW} first"
    return a


def all_sites() -> list[dict]:
    """Every source and watched page, and every studio with a jobs page
    (read once when asked; excluded sites are never here)."""
    from vs import studios as studios_mod
    p = prefs()
    out = [{"kind": "source", "id": sid, "name": cfg.get("org") or cfg.get("name") or sid, "url": cfg.get("url", ""),
            "keys": _site_keys(sid.replace("-", " "), cfg.get("org") or "", cfg.get("name") or "")}
           for sid, cfg in p.get("sources", {}).items() if cfg.get("enabled", True)]
    out += [{"kind": "source", "id": f"watch:{w['name']}", "name": w["name"], "url": w["url"], "keys": _site_keys(w["name"])}
            for w in p.get("watchlist", []) if w.get("enabled", True)]
    watched = {s["url"] for s in out}
    out += [{"kind": "studio", "id": f"watch:{s['name']}", "name": s["name"], "url": s["jobs_url"], "keys": _site_keys(s["name"])}
            for s in studios_mod.load() if s.get("jobs_url") and s["jobs_url"] not in watched]
    return out


def named_sites(question: str) -> list[dict]:
    """The sites a question names: "is Accurat hiring?" -> Accurat; "fablab"
    -> every Fab Lab. Matched on single words and on 2-3 words run together."""
    w = _words(question)
    grams = set(w) | {"".join(w[i:i + n]) for n in (2, 3) for i in range(len(w) - n + 1)}
    return [{k: v for k, v in site.items() if k != "keys"} for site in all_sites() if site["keys"] & grams]
