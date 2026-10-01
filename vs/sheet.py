"""The Google Sheet is the master list of sources (your decision, 1 Oct 2026).

One row per entry, `kind` = source | watch | studio | excluded. The app reads
the sheet's public CSV export at most every few minutes, keeps a local copy in
data/sheet-cache.csv (used when Google can't be reached), and falls back to
preferences.toml when the sheet is empty or unreadable.

    uv run python -m vs.sheet export   -> output/sources-for-sheet.csv (to import)
    uv run python -m vs.sheet check    -> what the app reads from the sheet now
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

COLUMNS = ["kind", "id", "name", "enabled", "url", "how_read", "settings", "weight", "opportunity_type",
           "tags", "city", "country", "studio_types", "description", "jobs_url", "notes"]
# Settings that are common columns rather than part of the JSON blob.
COMMON = {"adapter", "url", "weight", "kind_hint", "enabled", "name", "tags"}
TTL_S = 300
SNAPSHOT = Path(__file__).resolve().parent.parent / "sources-snapshot.csv"

_cache: dict = {"at": 0.0, "rows": None, "error": None}


def _cache_path() -> Path:
    from vs.config import DATA_DIR
    return DATA_DIR / "sheet-cache.csv"


def csv_url(sheet_url: str) -> str:
    """Turn a normal sheet link into its CSV export link."""
    if "/export" in sheet_url:
        return sheet_url
    base = sheet_url.split("/edit")[0].rstrip("/")
    return f"{base}/export?format=csv&gid=0"


def _parse(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if r.get("kind") and (r.get("url") or r.get("name")):
            rows.append(r)
    return rows


def rows(sheet_cfg: dict) -> tuple[list[dict] | None, str | None]:
    """(rows, error). rows is None when nothing usable is available."""
    now = time.time()
    if _cache["rows"] is not None and now - _cache["at"] < TTL_S:
        return _cache["rows"], _cache["error"]
    error = None
    try:
        import httpx
        r = httpx.get(csv_url(sheet_cfg["url"]), follow_redirects=True, timeout=20)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
        parsed = _parse(text)
        if parsed:
            _cache_path().write_text(text, encoding="utf-8")
        else:
            error = "the sheet is empty: using the previous copy or preferences.toml"
    except Exception as e:  # network, permissions...
        parsed, error = [], f"could not read the sheet ({type(e).__name__}): using the last saved copy"
    if not parsed and _cache_path().exists():
        parsed = _parse(_cache_path().read_text(encoding="utf-8"))
    if not parsed and SNAPSHOT.exists():  # first launch with no network: the copy shipped with the app
        parsed = _parse(SNAPSHOT.read_text(encoding="utf-8-sig"))
    result = parsed or None
    _cache.update(at=now, rows=result, error=error)
    return result, error


def _truthy(v: str) -> bool:
    return (v or "true").strip().lower() not in ("false", "no", "0", "off", "n")


def apply_to_prefs(data: dict) -> dict:
    """Replace sources / watchlist / studios in the preferences with the sheet's."""
    import os
    sheet_cfg = data.get("sheet", {})
    if not sheet_cfg.get("enabled") or not sheet_cfg.get("url") or os.environ.get("VS_NO_SHEET"):
        return data
    got, error = rows(sheet_cfg)
    data["_sheet_error"] = error
    if not got:
        return data
    sources, watch, studios, excluded = {}, [], [], []
    for r in got:
        kind = r["kind"].lower()
        try:
            extra = json.loads(r["settings"]) if r.get("settings") else {}
        except json.JSONDecodeError:
            extra = {}
            data["_sheet_error"] = f"row '{r.get('name') or r.get('id')}': the settings column is not valid JSON (ignored)"
        tags = [t.strip() for t in (r.get("tags") or "").split(",") if t.strip()]
        if kind == "source":
            sid = r.get("id") or r.get("name")
            cfg = {**extra, "adapter": r.get("how_read") or extra.get("adapter", "html_list"), "enabled": _truthy(r.get("enabled"))}
            if r.get("url"):
                cfg["url"] = r["url"]
            if r.get("weight"):
                try:  # Italian/Dutch sheets may write 0,9
                    cfg["weight"] = float(r["weight"].replace(",", "."))
                except ValueError:
                    pass
            if r.get("opportunity_type"):
                cfg["kind_hint"] = r["opportunity_type"]
            sources[sid] = cfg
        elif kind == "watch" and _truthy(r.get("enabled")):
            watch.append({"name": r.get("name") or r["url"], "url": r["url"], "tags": tags})
        elif kind == "studio":
            studios.append({"name": r.get("name", ""), "url": r.get("url", ""), "city": r.get("city", ""),
                            "country": r.get("country", ""), "description": r.get("description", ""),
                            "types": [t.strip() for t in (r.get("studio_types") or "").split(",") if t.strip()],
                            **({"jobs_url": r["jobs_url"]} if r.get("jobs_url") else {})})
        elif kind == "excluded":
            excluded.append({"name": r.get("name", ""), "url": r.get("url", ""), "notes": r.get("notes", "")})
    if sources:
        data["sources"] = sources
    if watch:
        data["watchlist"] = watch
    data["_studios"] = studios
    data["_excluded"] = excluded
    data["_from_sheet"] = True
    return data


# ---------------------------------------------------------------- export

EXCLUDED = [
    ("TransArtists (other open calls)", "https://www.transartists.org/en/other-open-calls", "403 to automated requests; its own calls page is read instead"),
    ("GIJN jobs", "https://gijn.org/jobs/", "403"),
    ("Dezeen Jobs", "https://www.dezeenjobs.com/feed/", "RSS answers 403"),
    ("Mostrami", "https://www.mostra-mi.it/news/bandi", "no usable feed"),
    ("LinkedIn", "https://www.linkedin.com/jobs/", "excluded by your decision (terms of service)"),
    ("Picter", "https://submit.picter.com/", "bot protection (Vercel checkpoint, 429): add calls with the bookmarklet"),
    ("Behance Jobs", "https://www.behance.net/joblist", "403"),
    ("Creativepool", "https://creativepool.com/jobs/", "403"),
    ("Coroflot", "https://www.coroflot.com/design-jobs", "403"),
    ("Working Not Working", "https://workingnotworking.com/jobs", "403"),
    ("It's Nice That jobs", "https://www.itsnicethat.com/jobs", "page gone (404)"),
    ("InfoJobs Italia", "https://www.infojobs.it/", "JavaScript-only"),
    ("Werken voor Nederland", "https://www.werkenvoornederland.nl/vacatures", "JavaScript-only"),
    ("UNjobs.org", "https://unjobs.org/", "robots.txt disallows"),
    ("UNDP jobs", "https://jobs.undp.org/", "robots.txt disallows"),
    ("UNESCO RSS", "https://careers.unesco.org/services/rss/", "robots.txt disallows (listing page read instead)"),
    ("Devex jobs", "https://www.devex.com/jobs/search", "403"),
    ("Varrgo", "https://varrgo.com/", "JavaScript-only"),
    ("Metabolic vacancies", "https://www.metabolic.nl/about/", "list loaded by a script; projects used as references"),
    ("unibz positions", "https://www.unibz.it/en/home/position-calls", "JavaScript search behind bot protection; EURAXESS covers unibz research posts"),
    ("Forensis", "https://forensis.org/", "domain now redirects to an unrelated site"),
    ("Earshot", "https://earshot.ngo/", "JavaScript-only"),
    ("V2_", "https://v2.nl/", "JavaScript-only, RSS malformed"),
    ("Goldsmiths Centre for Research Architecture", "https://www.gold.ac.uk/research-architecture/", "page gone (404)"),
    ("Culture Moves Europe (goethe.de)", "https://www.goethe.de/ins/be/en/kul/foe/cme.html", "403"),
    ("ADC Young Ones / One Club", "https://www.oneclub.org/", "robots.txt disallows"),
    ("IUAV", "https://www.iuav.it/", "JavaScript-only"),
    ("ISIA Urbino", "https://www.isiaurbino.net/", "invalid SSL certificate"),
    ("Regione Puglia PIN", "https://pin.regione.puglia.it/", "domain does not resolve"),
    ("Teatro Pubblico Pugliese", "https://www.teatropubblicopugliese.it/", "JavaScript-only"),
    ("The Pudding", "https://pudding.cool/", "no calls page"),
    ("dasauge", "https://dasauge.de/jobs/", "lists job categories, not postings (Indeed covers Germany)"),
    ("Yeast Photo Festival", "https://www.yeastphotofestival.it/", "JavaScript-only"),
    ("Unseen Amsterdam", "https://unseenphoto.com/", "JavaScript-only"),
    ("Foam (Foam Talent)", "https://www.foam.org/", "bot protection (Vercel checkpoint)"),
    ("Aperture", "https://aperture.org/", "403 (bot protection)"),
    ("Gazebook", "https://www.gazebook.it/", "403"),
    ("Les Rencontres d'Arles", "https://www.rencontres-arles.com/", "browser check (bot protection)"),
    ("FOMU Antwerp", "https://www.fomu.atomuseum.be/", "unreachable"),
    ("Amnesty International", "https://www.amnesty.org/en/careers/", "403 (national sections NL and Italy are watched)"),
    ("Human Rights Watch website", "https://www.hrw.org/", "403 (its jobs are read through Greenhouse instead)"),
    ("Article 19", "https://www.article19.org/", "403"),
    ("Free Press Unlimited", "https://www.freepressunlimited.org/", "403"),
    ("Mnemonic", "https://mnemonic.org/", "JavaScript-only"),
    ("Mevis & Van Deursen", "https://www.mevisvandeursen.nl/", "domain is now a casino site"),
    ("Tankboys", "https://www.tankboys.biz/", "domain is now a game site"),
    ("Studio Andrew Howard", "https://www.andrewhoward.net/", "domain expired"),
    ("Experimental Jetset", "https://www.experimentaljetset.nl/", "unreachable at this address: send the current link"),
    ("Haller Brun", "https://www.hallerbrun.com/", "unreachable at this address"),
    ("Our Polite Society", "https://www.ourpolitesociety.com/", "unreachable at this address"),
    ("Calibro", "https://calibro.pt/", "unreachable at this address"),
    ("Brave New Alps", "https://www.bravenewalps.com/", "unreachable at this address"),
    ("Building Paris", "https://www.building-paris.fr/", "unreachable at this address"),
    ("Change is good", "https://www.changeisgood.fr/", "unreachable at this address"),
    ("Barbara Says", "https://www.barbara-says.com/", "unreachable at this address"),
    ("Vandejong", "https://www.vandejong.nl/", "unreachable at this address"),
    ("Studio Laucke Siebein", "https://www.laucke-siebein.com/", "unreachable at this address"),
    ("Kummer & Herrman", "https://www.kummerherrman.nl/", "unreachable at this address"),
    ("Studio Fludd", "https://www.studiofludd.it/", "unreachable at this address"),
    ("Parco Studio", "https://www.parcostudio.it/", "unreachable at this address"),
    ("46xy (Mario Piazza)", "https://www.46xy.it/", "unreachable at this address"),
]


def export_rows() -> list[dict]:
    """Every current entry, from preferences.toml and studios.toml."""
    import tomllib

    from vs.config import APP_DIR, PREFS_PATH
    with open(PREFS_PATH, "rb") as f:
        p = tomllib.load(f)
    out = []
    for sid, cfg in p.get("sources", {}).items():
        extra = {k: v for k, v in cfg.items() if k not in COMMON}
        out.append({"kind": "source", "id": sid, "name": cfg.get("org") or sid, "enabled": "TRUE" if cfg.get("enabled", True) else "FALSE",
                    "url": cfg.get("url", ""), "how_read": cfg.get("adapter", ""),
                    "settings": json.dumps(extra, ensure_ascii=False) if extra else "",
                    "weight": cfg.get("weight", ""), "opportunity_type": cfg.get("kind_hint", "")})
    for w in p.get("watchlist", []):
        out.append({"kind": "watch", "name": w["name"], "enabled": "TRUE", "url": w["url"], "how_read": "watch",
                    "tags": ", ".join(w.get("tags", []))})
    studios_path = APP_DIR / "studios.toml"
    if studios_path.exists():
        with open(studios_path, "rb") as f:
            for s in tomllib.load(f).get("studio", []):
                out.append({"kind": "studio", "name": s["name"], "url": s["url"], "city": s.get("city", ""),
                            "country": s.get("country", ""), "studio_types": ", ".join(s.get("types", [])),
                            "description": s.get("description", ""), "jobs_url": s.get("jobs_url", "")})
    for name, url, why in EXCLUDED:
        out.append({"kind": "excluded", "name": name, "url": url, "enabled": "FALSE", "notes": why})
    return out


def export_csv(path: Path) -> int:
    rows_out = export_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows_out:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    return len(rows_out)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from vs.config import OUTPUT_DIR, prefs
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        n = export_csv(OUTPUT_DIR / "sources-for-sheet.csv")
        print(f"{n} rows -> {OUTPUT_DIR / 'sources-for-sheet.csv'}")
    else:
        p = prefs()
        print("from sheet:", p.get("_from_sheet", False), "| error:", p.get("_sheet_error"))
        print(len(p.get("sources", {})), "sources,", len(p.get("watchlist", [])), "watched pages,",
              len(p.get("_studios", [])), "studios,", len(p.get("_excluded", [])), "excluded")
