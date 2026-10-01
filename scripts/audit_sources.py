"""Phase 0 source audit: for each candidate listing page, record HTTP status,
robots.txt permission, advertised RSS/Atom feeds, and whether the page is
server-rendered (has many links in the raw HTML) or needs a browser.

    uv run python scripts/audit_sources.py > docs/source-audit.txt
"""

import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

UA = "vacancies-searcher/0.1 (personal, non-commercial source audit)"

CANDIDATES = {
    "on-the-move": "https://on-the-move.org/news",
    "transartists-calls": "https://www.transartists.org/en/other-open-calls",
    "resartis-calls": "https://resartis.org/open-calls/",
    "euraxess": "https://euraxess.ec.europa.eu/jobs/search",
    "eu-ft-api": "https://api.tech.ec.europa.eu/search-api/prod/rest/search?apiKey=SEDIA&text=***&pageSize=1&pageNumber=1",
    "mostrami-bandi": "https://www.mostra-mi.it/news/bandi",
    "exibart-bandi": "https://www.exibart.com/bandi-e-concorsi/",
    "giovaniartisti": "https://www.giovaniartisti.it/bandi",
    "artapartofculture": "https://www.artapartofculture.net/",
    "academictransfer": "https://www.academictransfer.com/en/jobs/",
    "artconnect": "https://www.artconnect.com/opportunities",
    "residency-unlimited": "https://residencyunlimited.org/opportunities/",
    "fontanel": "https://www.fontaneljobs.com/",
    "uiuxjobsboard": "https://uiuxjobsboard.com/design-jobs/netherlands",
    "dezeen-jobs": "https://www.dezeenjobs.com/",
    "gijn-jobs": "https://gijn.org/jobs/",
    "mondriaan": "https://www.mondriaanfonds.nl/en/",
    "creative-industries-fund": "https://stimuleringsfonds.nl/en/grants",
    "italian-council": "https://creativitacontemporanea.cultura.gov.it/",
    "e-flux": "https://www.e-flux.com/announcements/",
}

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")


def robots_ok(client: httpx.Client, url: str) -> str:
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        r = client.get(robots_url)
    except httpx.HTTPError as e:
        return f"robots unreachable ({type(e).__name__})"
    if r.status_code >= 400:
        return "no robots.txt (allowed)"
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return "allowed" if rp.can_fetch(UA, url) else "DISALLOWED"


def audit(client: httpx.Client, name: str, url: str) -> str:
    robots = robots_ok(client, url)
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return f"{name:26} ERROR {type(e).__name__}: {e} | robots: {robots}"
    ctype = r.headers.get("content-type", "").split(";")[0]
    if "json" in ctype or "xml" in ctype:
        return f"{name:26} {r.status_code} {ctype} {len(r.content)}B | robots: {robots}"
    tree = HTMLParser(r.text)
    feeds = [
        urljoin(str(r.url), n.attributes.get("href") or "")
        for n in tree.css("link[rel=alternate]")
        if (n.attributes.get("type") or "") in FEED_TYPES
    ]
    links = len(tree.css("a[href]"))
    words = len((tree.body.text(separator=" ") if tree.body else "").split())
    render = "static" if links > 40 and words > 300 else "maybe-JS"
    return (
        f"{name:26} {r.status_code} links={links} words={words} {render} | robots: {robots}"
        f" | feeds: {', '.join(feeds) or '-'} | final: {r.url}"
    )


def main() -> None:
    only = set(sys.argv[1:])
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20) as client:
        for name, url in CANDIDATES.items():
            if only and name not in only:
                continue
            print(audit(client, name, url), flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
