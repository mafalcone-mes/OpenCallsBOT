"""Offline parse tests on saved pages: if a site changes its layout, refresh
the fixture and these show what broke."""

from types import SimpleNamespace

import httpx

from conftest import FIXTURES
from vs.fetch import adapters


class FakeClient:
    def __init__(self, path, url):
        self.body = (FIXTURES / path).read_bytes()
        self.url = url

    def get(self, url, **kw):
        return httpx.Response(200, content=self.body, request=httpx.Request("GET", self.url))


def test_on_the_move_rss():
    items = adapters.rss(FakeClient("on-the-move.xml", "https://on-the-move.org/feed"), "on-the-move",
                         {"url": "https://on-the-move.org/feed"})
    assert len(items) >= 5
    assert all(i.url.startswith("https://on-the-move.org/") for i in items)
    assert not any(i.title.startswith("[") for i in items), "the [News] prefix must be stripped"
    assert not any("<a" in i.title for i in items)


def test_exibart_rss():
    items = adapters.rss(FakeClient("exibart.xml", "https://www.exibart.com/"), "exibart", {"url": "x"})
    assert len(items) >= 5 and all(i.title for i in items)


def test_resartis_listing():
    cfg = {"url": "https://resartis.org/open-calls/", "link_pattern": r"https://resartis\.org/open-call/[^/]+/", "max_items": 60}
    items = adapters.html_list(FakeClient("resartis.html", cfg["url"]), "resartis", cfg)
    assert len(items) >= 20
    assert all("/open-call/" in i.url and i.title for i in items)


def test_page_text_keeps_deadline_fields():
    from vs.pipeline import page_text

    eur = page_text((FIXTURES / "euraxess-job.html").read_text(encoding="utf-8"), "https://euraxess.ec.europa.eu/jobs/469019")
    assert "Application Deadline: 9 Nov 2026" in eur
    otm = page_text((FIXTURES / "otm-item.html").read_text(encoding="utf-8"),
                    "https://on-the-move.org/news/asymmetry-2027-assistant-curator-fellowship-nottingham-contemporary-uk")
    assert "Deadline: 25 October 2026" in otm
    assert "asymmetryart.org" in otm, "the official call link must be kept"
    assert "30 Sep 2026" not in otm, "other calls' deadlines from the sidebar must not leak in"


def test_euraxess_listing():
    cfg = {"url": "https://euraxess.ec.europa.eu/jobs/search", "fields": [22]}
    items = adapters.euraxess(FakeClient("euraxess.html", cfg["url"]), "euraxess", cfg)
    assert len(items) >= 5
    assert all(i.url.startswith("https://euraxess.ec.europa.eu/jobs/") for i in items)


def test_ifyoucould_listing():
    cfg = {"url": "https://www.ifyoucouldjobs.com/", "link_pattern": r"https://www\.ifyoucouldjobs\.com/jobs/\d+"}
    items = adapters.html_list(FakeClient("ifyoucould.html", cfg["url"]), "ifyoucould", cfg)
    assert len(items) >= 20
    assert all("/jobs/" in i.url and i.title for i in items)


def test_creative_lives_in_progress_listing():
    cfg = {"url": "https://creativelivesinprogress.com/opportunities",
           "link_pattern": r"https://creativelivesinprogress\.com/opportunities/[^/?#]+"}
    items = adapters.html_list(FakeClient("clip.html", cfg["url"]), "creative-lives-in-progress", cfg)
    assert len(items) >= 10
    assert all("/opportunities/" in i.url and i.title for i in items)


def test_weworkremotely_rss():
    items = adapters.rss(FakeClient("wwr-design.xml", "https://weworkremotely.com/"), "weworkremotely", {"url": "x"})
    assert len(items) >= 5
    assert all(i.url.startswith("https://weworkremotely.com/") and i.title for i in items)
