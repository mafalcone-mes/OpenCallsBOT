"""Find where an organisation publishes its opportunities, starting from its
home page and following only links found there (never guessed URLs).

For each organisation: robots.txt, RSS/Atom feeds, the candidate pages
(jobs, open calls, grants, residencies, fellowships, competitions...), whether
each candidate is readable without JavaScript, and how many item links it has.

    uv run python scripts/discover_sources.py [name ...] > docs/discovery.txt
"""

import collections
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

UA = "vacancies-searcher/0.1 (personal, non-commercial)"
OPP = re.compile(
    r"vacanc|vacature|\bjobs?\b|career|werken-bij|work-with-us|join-us|join our|hiring|open.?call|calls?\b|bandi|bando|"
    r"opportunit|grants?\b|funding|fund\b|subsid|residen|fellowship|scholarship|award|prize|premio|competition|concorso|"
    r"apply|application|candidatur|stage|intern|tirocin|assegn|phd|dottorat|call-for|submission|open-application|mobility",
    re.I)
FEED_TYPES = ("rss", "atom")

ORGS = {
    # 1. visual journalism
    "ProPublica": "https://www.propublica.org/",
    "Journalismfund Europe": "https://www.journalismfund.eu/",
    "European Journalism Centre": "https://ejc.net/",
    "Villamedia (NL journalism jobs)": "https://www.villamedia.nl/",
    "The Pudding": "https://pudding.cool/",
    # 2. investigative / spatial research
    "Forensis": "https://forensis.org/",
    "Border Forensics": "https://www.borderforensics.org/",
    "SITU": "https://situ.nyc/",
    "Earshot": "https://earshot.ngo/",
    "Airwars": "https://airwars.org/",
    "Goldsmiths CRA": "https://www.gold.ac.uk/research-architecture/",
    # 3. art + science + technology
    "S+T+ARTS": "https://starts.eu/",
    "Ars Electronica": "https://ars.electronica.art/",
    "Akademie Schloss Solitude": "https://www.akademie-solitude.de/en/",
    "V2_": "https://v2.nl/",
    "Cittadellarte": "https://www.cittadellarte.it/",
    "Triennale Milano": "https://triennale.org/",
    "MAXXI": "https://www.maxxi.art/",
    # 4. funding for individuals
    "Culture Moves Europe": "https://www.goethe.de/ins/be/en/kul/foe/cme.html",
    "GAI / Movin'Up": "https://www.giovaniartisti.it/",
    "Prince Claus Fund": "https://princeclausfund.org/",
    "European Cultural Foundation": "https://culturalfoundation.eu/",
    # 5. competitions
    "Malofiej": "https://www.malofiejgraphics.com/",
    "Information+": "https://informationplusconference.com/",
    "ADC Young Ones": "https://www.oneclub.org/youngones/",
    "D&AD New Blood": "https://www.dandad.org/",
    "ADI Design": "https://www.adi-design.org/",
    # 6. research positions at design schools
    "DensityDesign": "https://densitydesign.org/",
    "IUAV": "https://www.iuav.it/",
    "ISIA Urbino": "https://www.isiaurbino.net/",
    "KABK": "https://www.kabk.nl/en",
    "Sandberg Instituut": "https://sandberg.nl/",
    "Design Academy Eindhoven": "https://www.designacademy.nl/",
    # Puglia
    "Regione Puglia (PIN / giovani)": "https://pin.regione.puglia.it/",
    "Puglia Creativa": "https://www.pugliacreativa.it/",
    "Teatro Pubblico Pugliese": "https://www.teatropubblicopugliese.it/",
}

client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20)


def robots_for(url: str):
    p = urlparse(url)
    try:
        r = client.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400 or "<html" in r.text[:300].lower():
        return None
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp


def item_links(html: str, base: str) -> tuple[int, str]:
    """How many same-site links share the most common deeper path shape
    (a listing usually has many /something/<slug> links)."""
    t = HTMLParser(html)
    host = urlparse(base).netloc
    shapes = collections.Counter()
    sample = {}
    for a in t.css("a[href]"):
        h = urljoin(base, a.attributes.get("href") or "").split("#")[0]
        u = urlparse(h)
        if u.netloc != host or u.path.count("/") < 2:
            continue
        shape = re.sub(r"\d+", "<n>", re.sub(r"/[^/]{8,}(?=/?$)", "/<slug>", u.path))
        shapes[shape] += 1
        sample.setdefault(shape, h)
    if not shapes:
        return 0, ""
    shape, n = shapes.most_common(1)[0]
    return n, f"{shape} e.g. {sample[shape]}"


def check(name: str, home: str) -> None:
    rp = robots_for(home)
    try:
        r = client.get(home)
    except httpx.HTTPError as e:
        print(f"\n## {name}  {home}\n   ERROR {type(e).__name__}: {e}")
        return
    t = HTMLParser(r.text)
    words = len(t.body.text().split()) if t.body else 0
    feeds = [urljoin(str(r.url), n.attributes.get("href") or "") for n in t.css("link[rel=alternate]")
             if any(x in (n.attributes.get("type") or "") for x in FEED_TYPES)]
    print(f"\n## {name}  {home} -> {r.status_code} {r.url}")
    print(f"   robots: {'none' if rp is None else 'present'} | links={len(t.css('a[href]'))} words={words}"
          f"{' | LIKELY JS-ONLY' if len(t.css('a[href]')) < 10 else ''} | feeds: {feeds[:3] or '-'}")
    host = urlparse(str(r.url)).netloc.removeprefix("www.")
    cands = {}
    for a in t.css("a[href]"):
        h = urljoin(str(r.url), a.attributes.get("href") or "").split("#")[0]
        label = re.sub(r"\s+", " ", a.text()).strip()[:45]
        if urlparse(h).netloc.removeprefix("www.").endswith(host) and h.rstrip("/") != str(r.url).rstrip("/") \
                and (OPP.search(urlparse(h).path) or OPP.search(label)):
            cands.setdefault(h, label)
    for h, label in list(cands.items())[:7]:
        time.sleep(1.2)
        allowed = rp is None or rp.can_fetch(UA, h)
        try:
            c = client.get(h)
            n, shape = item_links(c.text, str(c.url))
            ct = c.headers.get("content-type", "").split(";")[0]
            print(f"   {'ok ' if allowed else 'NO!'} {c.status_code} [{label}] {h}\n        items~{n} {shape[:110]} ({ct})")
        except httpx.HTTPError as e:
            print(f"   {'ok ' if allowed else 'NO!'} ERR [{label}] {h} {type(e).__name__}")
    if not cands:
        print("   (no opportunity-like links on the home page)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    only = set(sys.argv[1:])
    for name, url in ORGS.items():
        if only and name not in only:
            continue
        check(name, url)
        time.sleep(1.5)
