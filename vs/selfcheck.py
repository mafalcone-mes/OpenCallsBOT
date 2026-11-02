"""Does the compiled program work? (`Oppo.exe selfcheck`, run by
scripts/build_beta.py after each build). A compiler can leave out files that
libraries load by name at run time; this touches each of them and the
dashboard, so a broken build fails there instead of on a tester's computer.
Offline and quick; it never touches the person's files (VS_HOME is a
temporary folder when the build script runs it)."""

from __future__ import annotations

import traceback


def _checks():
    def dashboard():
        from fastapi.testclient import TestClient

        from vs.web.app import app
        c = TestClient(app)
        for path in ("/setup", "/static/app.css", "/favicon.svg"):
            r = c.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code}"

    def dates():
        import dateparser
        from dateparser.search import search_dates
        for text, lang in (("Deadline 3 December 2026", "en"), ("Scadenza 3 dicembre 2026", "it"),
                           ("Date limite 3 décembre 2026", "fr"), ("Frist 3. Dezember 2026", "de"),
                           ("Deadline 3 december 2026", "nl"), ("Plazo 3 de diciembre de 2026", "es")):
            assert search_dates(text, languages=[lang]), text
        assert dateparser.parse("2026-12-03")

    def language():
        from lingua import LanguageDetectorBuilder
        assert LanguageDetectorBuilder.from_all_languages().build().detect_language_of("Bando per artisti e designer")

    def pages():
        import trafilatura
        from selectolax.parser import HTMLParser
        html = "<html><body><article><h1>Open call</h1><p>" + "Artists and designers can apply. " * 20 + "</p></article></body></html>"
        assert trafilatura.extract(html)
        assert HTMLParser(html).css_first("h1").text() == "Open call"

    def files():
        import icalendar
        import pypdf  # noqa: F401
        icalendar.Calendar().to_ical()

    def job_boards():
        import jobspy  # noqa: F401
        import tls_client  # noqa: F401

    def app_modules():
        import vs.brief
        import vs.chat
        import vs.pipeline
        import vs.report  # noqa: F401

    return [dashboard, dates, language, pages, files, job_boards, app_modules]


def run() -> int:
    # Checking the build must not start the build machine's 5 days (vs/beta.py
    # records the first opening): only inside this process, which exits after.
    from vs import beta
    beta.status = lambda force=False: {"active": False, "over": ""}
    failed = 0
    for check in _checks():
        try:
            check()
            print(f"ok    {check.__name__}", flush=True)
        except Exception:
            failed += 1
            print(f"FAIL  {check.__name__}\n{traceback.format_exc()}", flush=True)
    print("selfcheck: " + ("all good" if not failed else f"{failed} failed"), flush=True)
    return 1 if failed else 0
