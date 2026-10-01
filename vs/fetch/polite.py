"""The only way the app talks to the web: robots.txt, per-domain delays,
conditional GET and a 30-day cache of fetched bodies."""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from vs import db
from vs.config import prefs, user_agent

TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref")


class Blocked(RuntimeError):
    """robots.txt disallows the URL, or the site answered 401/403/429."""


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith(TRACKING_PARAMS)]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, "", urlencode(query), ""))


def domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


class PoliteClient:
    def __init__(self) -> None:
        cfg = prefs().get("fetch", {})
        self.min_delay = float(cfg.get("min_delay_s", 3.0))
        self.max_delay = float(cfg.get("max_delay_s", 5.0))
        self.ua = user_agent()
        self.client = httpx.Client(headers={"User-Agent": self.ua, "Accept-Language": "en,it;q=0.8,nl;q=0.6,de;q=0.5,es;q=0.5"},
                                   follow_redirects=True, timeout=30)
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def close(self) -> None:
        self.client.close()

    # -- politeness

    def _lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        host = p.netloc
        if host not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.client.get(f"{p.scheme}://{host}/robots.txt", timeout=15)
                if r.status_code >= 400:
                    rp = None  # no robots.txt: allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(self.ua, url)

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)

    # -- requests

    def get(self, url: str, *, max_age: timedelta | None = None, method: str = "GET", **kwargs) -> httpx.Response | None:
        """Polite request. With max_age, a cached body younger than that is
        returned without touching the network."""
        if not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        with db.session() as conn:
            cached = conn.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        if cached and max_age and method == "GET":
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
            if age < max_age:
                return httpx.Response(cached["status"] or 200, text=cached["body"] or "", request=httpx.Request("GET", url))
        headers = dict(kwargs.pop("headers", {}) or {})
        if cached and method == "GET":
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        with self._lock(host):
            self._wait(host)
            try:
                for attempt in range(3):
                    r = self.client.request(method, url, headers=headers, **kwargs)
                    if r.status_code in (429, 503) and attempt < 2:
                        retry_after = r.headers.get("Retry-After", "")
                        time.sleep(min(60, int(retry_after)) if retry_after.isdigit() else 10 * (attempt + 1))
                        continue
                    break
            finally:
                self._last[host] = time.monotonic()
        if r.status_code == 304 and cached:
            with db.session() as conn:
                conn.execute("UPDATE http_cache SET fetched_at=? WHERE url=?", (db.now(), url))
            return httpx.Response(200, text=cached["body"] or "", request=r.request, headers={"x-from-cache": "304"})
        if r.status_code in (401, 403, 429):
            raise Blocked(f"{r.status_code} from {url}")
        ctype = r.headers.get("content-type", "")
        if method == "GET" and r.status_code == 200 and any(t in ctype for t in ("text", "xml", "json")):
            with db.session() as conn:
                conn.execute(
                    "INSERT INTO http_cache(url, etag, last_modified, fetched_at, status, body) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified, "
                    "fetched_at=excluded.fetched_at, status=excluded.status, body=excluded.body",
                    (url, r.headers.get("etag"), r.headers.get("last-modified"), db.now(), r.status_code, r.text),
                )
        return r

    def prune_cache(self, days: int = 30) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with db.session() as conn:
            conn.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cutoff,))
