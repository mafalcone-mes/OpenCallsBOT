"""Check candidate studios: does the site exist, what does it say about itself
(meta description), and does it have a jobs / internship page the app can
read? Follows only links found on the studio's own home page.

    uv run python scripts/check_studios.py > docs/studios-check.json
"""

import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

UA = "vacancies-searcher/0.1 (personal, non-commercial)"
JOBS = re.compile(r"\bjobs?\b|career|vacanc|vacature|vacatures|intern|stage|stagiair|tirocin|join|work.with|werken|lavora|"
                  r"collabora|open.?call|opportunit|hiring|we.?re looking|stellen|praktik|apply", re.I)

# (name, url, city, country, types)
CANDIDATES = [
    # Netherlands
    ("Studio Joost Grootens", "https://www.joostgrootens.nl/", "Amsterdam", "NL", ["information design", "editorial"]),
    ("Experimental Jetset", "https://www.experimentaljetset.nl/", "Amsterdam", "NL", ["cultural", "editorial"]),
    ("Irma Boom Office", "https://www.irmaboom.nl/", "Amsterdam", "NL", ["editorial", "books"]),
    ("Metahaven", "https://metahaven.net/", "Amsterdam", "NL", ["critical", "research"]),
    ("Thonik", "https://www.thonik.nl/", "Amsterdam", "NL", ["cultural", "political"]),
    ("Studio Dumbar", "https://studiodumbar.com/", "Rotterdam", "NL", ["identity"]),
    ("Mainstudio", "https://www.mainstudio.com/", "Amsterdam", "NL", ["cultural", "editorial"]),
    ("Raw Color", "https://www.rawcolor.nl/", "Eindhoven", "NL", ["cultural", "spatial"]),
    ("Studio Spass", "https://www.studiospass.com/", "Rotterdam", "NL", ["cultural", "spatial"]),
    ("Haller Brun", "https://www.hallerbrun.com/", "Amsterdam", "NL", ["cultural", "information design"]),
    ("Studio Moniker", "https://studiomoniker.com/", "Amsterdam", "NL", ["interactive", "critical"]),
    ("Our Polite Society", "https://www.ourpolitesociety.com/", "Amsterdam", "NL", ["cultural", "type"]),
    ("Trapped in Suburbia", "https://www.trappedinsuburbia.com/", "The Hague", "NL", ["participatory", "interactive"]),
    ("The Rodina", "https://www.therodina.com/", "Amsterdam", "NL", ["critical", "interactive"]),
    ("Kossmanndejong", "https://www.kossmanndejong.nl/", "Amsterdam", "NL", ["exhibition"]),
    ("Studio Frank Bruggeman", "https://www.frankbruggeman.nl/", "Amsterdam", "NL", ["cultural", "spatial"]),
    # Italy
    ("The Visual Agency", "https://thevisualagency.com/", "Milan", "IT", ["information design"]),
    ("Calibro", "https://calibro.pt/", "Lisbon/Milan", "PT", ["information design"]),
    ("Leftloft", "https://www.leftloft.com/", "Milan", "IT", ["identity", "type", "cultural"]),
    ("Studio FM Milano", "https://www.studiofmmilano.it/", "Milan", "IT", ["cultural", "editorial", "exhibition"]),
    ("Zaven", "https://www.zaven.net/", "Venice", "IT", ["cultural", "exhibition"]),
    ("Tankboys", "https://www.tankboys.biz/", "Venice", "IT", ["cultural", "editorial"]),
    ("Studio Mut", "https://www.studiomut.com/", "Bolzano", "IT", ["cultural", "editorial"]),
    ("Lupo & Burtscher", "https://www.lupoburtscher.it/", "Bolzano", "IT", ["cultural", "exhibition"]),
    ("Brave New Alps", "https://www.bravenewalps.com/", "Rovereto", "IT", ["social", "participatory", "research"]),
    ("Dinamo Milano", "https://www.dinamomilano.com/", "Milan", "IT", ["cultural", "exhibition"]),
    ("Leonardo Sonnoli / Tassinari Vetta", "https://www.tassinarivetta.it/", "Trieste", "IT", ["cultural", "type"]),
    ("Studio Temp", "https://www.studiotemp.it/", "Milan", "IT", ["cultural", "editorial"]),
    ("Tomo Tomo", "https://www.tomotomo.it/", "Bologna", "IT", ["cultural", "editorial"]),
    # Rest of Europe
    ("OSP Open Source Publishing", "https://osp.kitchen/", "Brussels", "BE", ["critical", "open source", "editorial"]),
    ("Base Design", "https://www.basedesign.com/", "Brussels", "BE", ["identity", "cultural"]),
    ("Bureau Borsche", "https://www.bureauborsche.com/", "Munich", "DE", ["cultural", "editorial"]),
    ("HORT", "https://www.hort.org.uk/", "Berlin", "DE", ["cultural"]),
    ("Studio Yukiko", "https://www.studio-yukiko.com/", "Berlin", "DE", ["editorial", "cultural"]),
    ("NODE Berlin Oslo", "https://www.nodeberlin.com/", "Berlin", "DE", ["cultural", "editorial", "exhibition"]),
    ("Atelier Brückner", "https://www.atelier-brueckner.com/", "Stuttgart", "DE", ["exhibition"]),
    ("Ralph Appelbaum Associates", "https://www.raai.com/", "London/Berlin", "UK", ["exhibition"]),
    ("Spin", "https://www.spin.co.uk/", "London", "UK", ["identity", "type"]),
    ("Kellenberger-White", "https://www.kellenberger-white.com/", "London", "UK", ["cultural", "type"]),
    ("Pentagram", "https://www.pentagram.com/", "London", "UK", ["identity", "information design"]),
    ("Building Paris", "https://www.building-paris.fr/", "Paris", "FR", ["cultural", "editorial"]),
    ("Vier5", "https://www.vier5.de/", "Paris", "FR", ["cultural", "type"]),
    ("Change is good", "https://www.changeisgood.fr/", "Paris", "FR", ["cultural", "political"]),
    ("Barbara Says", "https://www.barbara-says.com/", "Lisbon", "PT", ["cultural", "editorial"]),
    ("Folch", "https://www.folchstudio.com/", "Barcelona", "ES", ["editorial", "identity"]),
    ("Mucho", "https://wearemucho.com/", "Barcelona", "ES", ["identity", "cultural"]),
]
BOARDS = [
    ("BNO (Dutch designers' association)", "https://www.bno.nl/", "NL"),
    ("AIAP (Italian graphic design association)", "https://www.aiap.it/", "IT"),
    ("dasauge (German creative jobs)", "https://www.dasauge.de/jobs/", "DE"),
    ("Le Signe (Centre national du graphisme)", "https://www.centrenationaldugraphisme.fr/", "FR"),
    ("Graphisme en France / CNAP", "https://www.cnap.fr/", "FR"),
    ("Creative Belgium / Mediarte", "https://www.mediarte.be/", "BE"),
]

client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20)


def check(name, url):
    out = {"name": name, "url": url}
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        out["error"] = f"{type(e).__name__}"
        return out
    out["status"] = r.status_code
    out["final"] = str(r.url)
    t = HTMLParser(r.text)
    desc = t.css_first('meta[name="description"]') or t.css_first('meta[property="og:description"]')
    title = t.css_first("title")
    out["title"] = re.sub(r"\s+", " ", title.text()).strip()[:90] if title else ""
    out["description"] = re.sub(r"\s+", " ", (desc.attributes.get("content") or "")).strip()[:220] if desc else ""
    out["links"] = len(t.css("a[href]"))
    host = urlparse(str(r.url)).netloc.removeprefix("www.")
    rp = None
    try:
        rr = client.get(f"https://{urlparse(str(r.url)).netloc}/robots.txt", timeout=10)
        if rr.status_code < 400 and "<html" not in rr.text[:300].lower():
            rp = RobotFileParser(); rp.parse(rr.text.splitlines())
    except httpx.HTTPError:
        pass
    jobs = {}
    for a in t.css("a[href]"):
        h = urljoin(str(r.url), a.attributes.get("href") or "").split("#")[0]
        label = re.sub(r"\s+", " ", a.text()).strip()[:40]
        if urlparse(h).netloc.removeprefix("www.") == host and (JOBS.search(urlparse(h).path) or JOBS.search(label)):
            if h.rstrip("/") != str(r.url).rstrip("/") and not h.startswith("mailto"):
                jobs.setdefault(h, label)
    checked = []
    for h, label in list(jobs.items())[:4]:
        time.sleep(0.8)
        try:
            jr = client.get(h)
            checked.append({"url": h, "label": label, "status": jr.status_code,
                            "allowed": rp is None or rp.can_fetch(UA, h), "words": len(HTMLParser(jr.text).text().split())})
        except httpx.HTTPError as e:
            checked.append({"url": h, "label": label, "error": type(e).__name__})
    out["jobs_pages"] = checked
    return out


CALLS = re.compile(r"open.?call|calls?|grant|award|prize|premio|bando|concorso|submission|submit|residen|"
                   r"fellowship|portfolio review|talent|applica|apply|candidatur|partecipa|opportunit", re.I)


def run_list(path: str, pattern) -> list:
    """Check a JSON list of {"name", "url", ...} with a custom link pattern."""
    global JOBS
    JOBS = pattern
    out = []
    for item in json.load(open(path, encoding="utf-8")):
        res = check(item["name"], item["url"])
        res.update({k: v for k, v in item.items() if k not in ("name", "url")})
        out.append(res)
        time.sleep(1)
    return out


if __name__ == "__main__" and len(sys.argv) > 2:
    sys.stdout.reconfigure(encoding="utf-8")
    pattern = CALLS if sys.argv[1] == "calls" else JOBS
    print(json.dumps(run_list(sys.argv[2], pattern), indent=1, ensure_ascii=False))
    sys.exit(0)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    results = {"studios": [], "boards": []}
    for name, url, city, country, types in CANDIDATES:
        res = check(name, url)
        res.update(city=city, country=country, types=types)
        results["studios"].append(res)
        time.sleep(1)
    for name, url, country in BOARDS:
        res = check(name, url)
        res["country"] = country
        results["boards"].append(res)
        time.sleep(1)
    print(json.dumps(results, indent=1, ensure_ascii=False))
