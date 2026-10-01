"""Built-in adapters, chosen per source by `adapter = "..."` in preferences.toml.
Adding a source of an existing kind is configuration only."""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from typing import Callable, Iterable

import feedparser
from selectolax.parser import HTMLParser

from vs.fetch import RawItem
from vs.fetch.polite import PoliteClient


def strip_html(value: str) -> str:
    if not value:
        return ""
    text = HTMLParser(value).text(separator=" ") if "<" in value else value
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def rss(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """An RSS/Atom feed. With `queries` + `query_param`, the feed is fetched
    once per query (e.g. ReliefWeb's ?search=...) and results are merged."""
    entries, seen = [], set()
    queries = cfg.get("queries") or [None]
    for q in queries:
        params = {cfg.get("query_param", "search"): q} if q else None
        r = client.get(cfg["url"], params=params)
        feed = feedparser.parse(r.content if r is not None else b"")
        if feed.bozo and not feed.entries:
            raise ValueError(f"Feed did not parse: {feed.bozo_exception}")
        for e in feed.entries:
            if e.get("link") not in seen:
                seen.add(e.get("link"))
                entries.append(e)
    full_feed = bool(cfg.get("full_text_in_feed"))
    items = []
    for e in entries:
        title = re.sub(r"^\[[^\]]+\]\s*", "", strip_html(e.get("title", "")))  # On the Move prefixes "[News]"
        content = ""
        if e.get("content"):
            content = strip_html(e["content"][0].get("value", ""))
        snippet = strip_html(e.get("summary", ""))
        if not content and len(snippet) > 800:  # e.g. ReliefWeb puts the whole job in the summary
            content = snippet
        link = e.get("link") or ""
        if not link or not title:
            continue
        items.append(RawItem(
            url=link, title=title, source_id=source_id, snippet=snippet[:1500],
            # Many feeds carry only an excerpt (WordPress often the first part
            # of the post), so the page is read unless the source says its feed
            # holds the whole text (full_text_in_feed = true, e.g. ReliefWeb).
            text=content if full_feed and len(content) > 800 else "",
            published=e.get("published") or e.get("updated"),
            needs_detail=not (full_feed and len(content) > 800),
        ))
    return _require(items, cfg)


def _require(items: list[RawItem], cfg: dict) -> list[RawItem]:
    """For news-heavy feeds: keep only items whose title or summary matches
    `require` (a regex), so the model doesn't read every article."""
    pattern = cfg.get("require")
    if not pattern:
        return items
    rx = re.compile(pattern, re.I)
    return [it for it in items if rx.search(f"{it.title} {it.snippet}")]


EU_STATUS_OPEN_FORTHCOMING = ["31094501", "31094502"]


def eu_ft(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """EU Funding & Tenders Portal search API (anonymous, read-only)."""
    query = {"bool": {"must": [{"terms": {"type": ["1", "2", "8"]}},
                               {"terms": {"status": EU_STATUS_OPEN_FORTHCOMING}}]}}
    files = {"query": ("blob", json.dumps(query), "application/json"),
             "languages": ("blob", '["en"]', "application/json")}
    today = date.today().isoformat()
    seen, items = set(), []
    for text in cfg.get("queries", []):
        url = ("https://api.tech.ec.europa.eu/search-api/prod/rest/search"
               f"?apiKey=SEDIA&text={text.replace(' ', '%20')}&pageSize=50&pageNumber=1")
        r = client.get(url, method="POST", files=files)
        for res in r.json().get("results", []):
            m = res.get("metadata", {})
            first = lambda k: (m.get(k) or [None])[0]  # noqa: E731
            deadline = (first("deadlineDate") or "")[:10]
            ident = first("identifier") or res.get("url")
            if ident in seen or (deadline and deadline < today):
                continue  # the API's status filter is unreliable; enforce the date here
            seen.add(ident)
            title = first("title") or ident
            conditions = strip_html(first("topicConditions") or "")[:1500]
            desc = strip_html(first("descriptionByte") or "") or strip_html(res.get("content") or "")
            page = f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{ident}"
            items.append(RawItem(
                url=page, title=title, source_id=source_id,
                snippet=f"{title}. {', '.join(m.get('keywords') or [])}",
                text=f"{title}\nIdentifier: {ident}\nDeadline: {deadline}\nType of action: {first('typesOfAction') or ''}\n"
                     f"Programme period: {first('programmePeriod') or ''}\n\n{desc}\n\n{conditions}",
                hints={"deadline": deadline or None, "org_name": "European Commission (EU Funding & Tenders)",
                       "for_organisations": True},
                needs_detail=False,
            ))
    return items


def _link_text(a) -> str:
    """Readable text of a listing link. Prefers the card's heading, and rejoins
    words some sites split into one element per letter (animated titles)."""
    def collapse(t: str) -> str:
        return re.sub(r"\s+", " ", t or "").strip()

    heading = a.css_first("h1, h2, h3, h4")
    node = heading or a
    words = []
    for el in node.traverse(include_text=False):
        kids = list(el.iter(include_text=False))
        if len(kids) > 1 and all(len(k.text(strip=True)) <= 1 for k in kids):
            words.append("".join(k.text(strip=True) for k in kids))
    if words and heading is not None:
        return " ".join(words)
    return collapse(node.text(strip=False))


def html_list(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """A listing page whose item links match `link_pattern` (a regex on the
    absolute URL). The title is the longest link text seen for that link."""
    from urllib.parse import urljoin

    pattern = re.compile(cfg["link_pattern"])
    titles: dict[str, str] = {}
    pages = cfg.get("urls") or [cfg["url"]]  # `urls`: several listing pages, e.g. one per country
    errors = []
    for page_url in pages:
        try:
            r = client.get(page_url)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"{page_url}: {e}")
            continue  # one missing country page shouldn't sink the source
        tree = HTMLParser(r.text)
        for a in tree.css("a[href]"):
            href = urljoin(str(r.url), a.attributes.get("href") or "")
            if pattern.fullmatch(href):
                text = _link_text(a)
                if len(text) > len(titles.get(href, "")):
                    titles[href] = text
    if errors and len(errors) == len(pages):
        raise RuntimeError("Every listing page failed: " + " | ".join(errors[:3]))
    items = []
    for href, text in titles.items():
        if not text:  # image-only links: derive a readable title from the slug
            text = href.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()
        items.append(RawItem(url=href, title=text, source_id=source_id, snippet=text))
    return _require(items, cfg)[: int(cfg.get("max_items", 80))]


def un_careers(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """UN Secretariat job openings (careers.un.org), via the public JSON API
    its own search page uses. One request per keyword."""
    api = "https://careers.un.org/api/public/opening/jo/list/filteredV2/en"
    items, seen = [], set()
    for kw in cfg.get("queries", []):
        body = {"filterConfig": {"keyword": kw},
                "pagination": {"page": 0, "itemPerPage": int(cfg.get("per_query", 25)), "sortBy": "startDate", "sortDirection": -1}}
        r = client.get(api, method="POST", json=body)
        for j in (r.json().get("data") or {}).get("list", []):
            jid = str(j.get("jobId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            stations = j.get("dutyStation") or []
            where = ", ".join(s.get("description", "") for s in stations if isinstance(s, dict))
            dept = (j.get("dept") or {}).get("name", "") if isinstance(j.get("dept"), dict) else ""
            deadline = (j.get("endDate") or "")[:10]
            title = j.get("postingTitle") or j.get("jobTitle") or ""
            desc = strip_html(j.get("jobDescription") or "")
            items.append(RawItem(
                url=f"https://careers.un.org/jobSearchDescription/{jid}?language=en", title=title, source_id=source_id,
                snippet=f"{title}. {dept}. {where}. {desc[:500]}",
                text=f"{title}\nOrganisation: United Nations Secretariat, {dept}\nDuty station: {where}\n"
                     f"Level: {j.get('jobLevel', '')}\nDeadline: {deadline}\n\n{desc}",
                hints={"deadline": deadline or None, "org_name": f"United Nations ({dept})" if dept else "United Nations"},
                needs_detail=False,
            ))
    return items


def taleo(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """Oracle Taleo career sites (WHO, FAO and many UN agencies/NGOs). The
    listing comes from the JSON service the site's own search page calls;
    detail pages are read by page_text, which decodes Taleo's embedded text."""
    import re as _re

    base, section = cfg["base"].rstrip("/"), cfg["section"]
    page = client.get(f"{base}/careersection/{section}/jobsearch.ftl?lang=en")
    portal = _re.search(r"portal=(\d+)", page.text)
    if not portal:
        raise ValueError("Taleo portal id not found on the search page")
    items, seen = [], set()
    for page_no in range(1, int(cfg.get("pages", 3)) + 1):
        body = {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": "", "CATEGORY": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []}, "pageNo": page_no}
        r = client.get(f"{base}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal.group(1)}",
                       method="POST", json=body, headers={"tz": "GMT+01:00"})
        reqs = r.json().get("requisitionList", [])
        if not reqs:
            break
        for req in reqs:
            no = req.get("contestNo")
            if not no or no in seen:
                continue
            seen.add(no)
            cols = req.get("column") or []
            title = cols[0] if cols else no
            where = ", ".join(json.loads(cols[1])) if len(cols) > 1 and cols[1].startswith("[") else (cols[1] if len(cols) > 1 else "")
            items.append(RawItem(
                url=f"{base}/careersection/{section}/jobdetail.ftl?job={no}&lang=en", title=title, source_id=source_id,
                snippet=f"{title}. {cfg.get('org', '')}. {where}",
                hints={"org_name": cfg.get("org")},
            ))
    return items


def ats(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """Organisations that publish jobs through a job system with an official
    public feed (made for embedding job lists): `system` = greenhouse | bamboohr,
    `board` = the organisation's id in that system. Full text included."""
    system, board, org = cfg["system"], cfg["board"], cfg.get("org", "")
    items = []
    if system == "greenhouse":
        r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
        for j in r.json().get("jobs", []):
            text = strip_html(html.unescape(j.get("content") or ""))
            loc = (j.get("location") or {}).get("name", "")
            items.append(RawItem(url=j["absolute_url"], title=j.get("title", ""), source_id=source_id,
                                 snippet=f"{j.get('title')}. {org}. {loc}", text=f"{j.get('title')}\nOrganisation: {org}\nLocation: {loc}\n\n{text}",
                                 hints={"org_name": org or j.get("company_name")}, needs_detail=False))
    elif system == "bamboohr":
        r = client.get(f"https://{board}.bamboohr.com/careers/list", headers={"Accept": "application/json"})
        for j in r.json().get("result", []):
            jid = j.get("id")
            d = client.get(f"https://{board}.bamboohr.com/careers/{jid}/detail", headers={"Accept": "application/json"})
            jo = (d.json().get("result") or {}).get("jobOpening", {}) if d.status_code == 200 else {}
            loc = ", ".join(filter(None, [(j.get("atsLocation") or {}).get("city"), (j.get("atsLocation") or {}).get("country")]))
            title = j.get("jobOpeningName", "")
            items.append(RawItem(url=jo.get("jobOpeningShareUrl") or f"https://{board}.bamboohr.com/careers/{jid}", title=title,
                                 source_id=source_id, snippet=f"{title}. {org}. {loc}",
                                 text=f"{title}\nOrganisation: {org}\nLocation: {loc}\nType: {j.get('employmentStatusLabel', '')}\n\n"
                                      f"{strip_html(jo.get('description') or '')}",
                                 hints={"org_name": org}, needs_detail=not jo))
    else:
        raise ValueError(f"Unknown job system: {system}")
    return items


def euraxess(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """EURAXESS ignores free-text keywords in plain HTTP requests, so it is
    queried by research-field facet ids (see docs/sources.md)."""
    items, seen = [], set()
    requests = [{"f[0]": f"job_research_field:{fid}", "page": str(page)}
                for fid in cfg.get("fields", []) for page in range(int(cfg.get("pages", 1)))]
    for params in requests or [{}]:
        r = client.get(cfg["url"], params=params)
        tree = HTMLParser(r.text)
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            if not re.fullmatch(r"/jobs/\d+", href) or href in seen:
                continue
            seen.add(href)
            card = a.parent
            for _ in range(4):  # climb to the result card for a snippet
                if card is not None and len(card.text(strip=True)) < 200:
                    card = card.parent
            snippet = strip_html(card.html if card is not None else "")[:800]
            items.append(RawItem(url=f"https://euraxess.ec.europa.eu{href}", title=a.text(strip=True),
                                 source_id=source_id, snippet=snippet))
    return items


def jobspy_src(client: PoliteClient, source_id: str, cfg: dict) -> list[RawItem]:
    """Indeed via python-jobspy (Google Jobs in JobSpy is too brittle). LinkedIn is never used."""
    from jobspy import scrape_jobs

    from vs.config import prefs

    search = prefs().get("search", {})
    items: list[RawItem] = []
    # At most `max_searches` board requests per refresh (rate limits), rotating
    # through query x country pairs so every pair comes up over a few refreshes.
    # English titles are searched in every country, local-language titles
    # ([search.local]) only in their own country.
    local = search.get("local", {})
    pairs = [(c, q) for c in search.get("countries", []) for q in search.get("queries", []) + local.get(c, [])]
    budget = int(search.get("max_searches", 12))
    start = (date.today().toordinal() * budget) % max(1, len(pairs))
    chosen = (pairs[start:] + pairs[:start])[:budget]
    errors = []
    for country, q in chosen:
        try:
            df = scrape_jobs(
                site_name=["indeed"], search_term=q, country_indeed=country,
                location=country.title(), results_wanted=int(search.get("results_per_query", 25)),
                hours_old=int(search.get("hours_old", 336)), description_format="markdown", verbose=0,
            )
        except TypeError:
            raise  # an API mismatch with the installed JobSpy: never hide it
        except Exception as e:  # jobspy raises many kinds of errors on blocks
            if "429" in str(e) or "blocked" in str(e).lower():
                raise
            errors.append(f"{q} / {country}: {type(e).__name__}: {str(e)[:120]}")
            continue
        for row in df.to_dict("records"):
            url = row.get("job_url_direct") or row.get("job_url")
            if not url:
                continue
            desc = str(row.get("description") or "")
            loc = ", ".join(str(row.get(k)) for k in ("city", "state", "country") if row.get(k) and str(row.get(k)) != "nan")
            items.append(RawItem(
                url=str(row.get("job_url") or url), title=str(row.get("title") or ""), source_id=source_id,
                snippet=f"{row.get('title')} at {row.get('company')} ({loc}). {desc[:600]}",
                text=f"{row.get('title')}\nCompany: {row.get('company')}\nLocation: {loc}\n"
                     f"Remote: {row.get('is_remote')}\nSalary: {row.get('min_amount')}-{row.get('max_amount')} "
                     f"{row.get('currency')} {row.get('interval')}\n\n{desc}",
                hints={"org_name": str(row.get("company") or "") or None},
                needs_detail=len(desc) < 300,
            ))
    if errors and len(errors) == len(chosen):
        raise RuntimeError("Every job-board search failed: " + " | ".join(errors[:3]))
    return items


ADAPTERS: dict[str, Callable[[PoliteClient, str, dict], Iterable[RawItem]]] = {
    "rss": rss,
    "html_list": html_list,
    "eu_ft": eu_ft,
    "euraxess": euraxess,
    "un_careers": un_careers,
    "taleo": taleo,
    "ats": ats,
    "jobspy": jobspy_src,
}


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date().isoformat()
    except ValueError:
        return None
