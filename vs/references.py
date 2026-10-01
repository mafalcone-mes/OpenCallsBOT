"""Reference work: projects by studios you admire (e.g. an investigative
research agency, a data-design studio). They are not sources of vacancies: their project
descriptions are a picture of the work you want to do, used as positive taste
signals and shown to the model when it explains matches.

Saved as one Markdown file per project in data/references/<studio>/, so you
can delete the ones that don't represent you, or add your own by hand."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import yaml

from vs.config import DATA_DIR, prefs
from vs.fetch.adapters import strip_html
from vs.fetch.polite import PoliteClient

REF_DIR = DATA_DIR / "references"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "item"


def _path_for(folder, url: str):
    """One file per project address: reuse the file that already holds this
    URL, otherwise name it after the last part of the address (unique, unlike
    titles: many sites give every page the same title)."""
    for f in folder.glob("*.md"):
        head = f.read_text(encoding="utf-8")[:600]
        if f"url: {url}\n" in head or f"url: '{url}'\n" in head:
            return f
    tail = [part for part in url.split("?")[0].rstrip("/").split("/") if part][-1]
    return folder / f"{_slug(tail)}.md"


def _save(studio: str, title: str, url: str, text: str, meta: dict | None = None) -> bool:
    folder = REF_DIR / _slug(studio)
    folder.mkdir(parents=True, exist_ok=True)
    path = _path_for(folder, url)
    removed = folder / "_removed" / path.name
    if removed.exists():
        return False  # you removed it once: don't bring it back
    front = {"studio": studio, "title": title, "url": url,
             "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(meta or {})}
    body = f"---\n{yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=100)}---\n\n{text.strip()}\n"
    if path.exists() and path.read_text(encoding="utf-8").split("---", 2)[-1].strip() == text.strip():
        return False
    path.write_text(body, encoding="utf-8")
    return True


def _join_spelled(text: str) -> str:
    """Titles animated letter by letter (one <span> per letter) come out one
    letter per line: join those runs back into one line, drop blank lines."""
    out: list[str] = []
    run = ""
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        if len(line) == 1:
            run += line
            continue
        if run:
            out.append(run)
            run = ""
        out.append(line)
    if run:
        out.append(run)
    return "\n".join(out)


def _fa_api(client: PoliteClient, cfg: dict, log) -> int:
    data = client.get(cfg["url"]).json()
    n = 0
    for inv in data.get("investigations", [])[: int(cfg.get("max_projects", 150))]:
        names = lambda key: ", ".join(t.get("name") or t.get("post_title") or "" for t in inv.get(key) or [] if isinstance(t, dict))  # noqa: E731
        text = "\n".join(filter(None, [
            inv.get("title"),
            strip_html(inv.get("abstract") or ""),
            f"Areas: {names('investigation_area')}" if inv.get("investigation_area") else "",
            f"Methods: {names('methodologies')}" if inv.get("methodologies") else "",
            f"Location: {inv.get('location_title')}" if inv.get("location_title") else "",
            f"Presented in: {', '.join(inv.get('forum') or [])}" if inv.get("forum") else "",
        ]))
        url = f"https://forensic-architecture.org/investigation/{inv.get('slug')}"
        n += _save(cfg["name"], inv.get("title") or inv.get("slug"), url, text,
                   {"date": inv.get("publication_date") or ""})
    log(f"{cfg['name']}: {n} new or updated projects")
    return n


GENERIC_TITLES = {"write-up", "writeup", "home", "portfolio", "work", "projects", "project", "index", "untitled"}


def _project_title(tree, item, studio: str) -> str:
    """The project's name. Page titles are often generic (the studio's name
    on every page), so fall back to the main heading, the link text, then the
    address."""
    def generic(t: str) -> bool:
        t = (t or "").strip().lower()
        return len(t) < 3 or t in GENERIC_TITLES or t == studio.lower() or studio.lower() in t and len(t) < len(studio) + 4

    page = tree.css_first("title").text(strip=True) if tree.css_first("title") else ""
    page = re.split(r"\s[|–—-]\s", page)[0].strip()
    heading = tree.css_first("h1")
    heading = re.sub(r"\s+", " ", heading.text()).strip() if heading else ""
    slug = item.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()
    for candidate in (page, heading, item.title, slug):
        if not generic(candidate):
            return candidate[:150]
    return slug


def _html_list(client: PoliteClient, cfg: dict, log) -> int:
    from selectolax.parser import HTMLParser

    from vs.fetch.adapters import html_list
    from vs.pipeline import page_text

    items = html_list(client, "reference", {**cfg, "max_items": int(cfg.get("max_projects", 60)) * 2})
    exclude = re.compile(cfg["exclude_pattern"]) if cfg.get("exclude_pattern") else None
    items = [it for it in items if not (exclude and exclude.search(it.url))][: int(cfg.get("max_projects", 60))]
    n = 0
    for i, it in enumerate(items, 1):
        try:
            r = client.get(it.url)
        except Exception as e:
            log(f"  could not read {it.url}: {e}")
            continue
        tree = HTMLParser(r.text)
        title = _project_title(tree, it, cfg["name"])
        if cfg.get("content_selector"):  # keep only the project text, not the site menu
            # Every match is kept (a CSS list like "main, article" works on
            # malformed pages where the body ends up outside <main>). Plain
            # text: trafilatura discards fragments like these.
            parts = []
            for node in tree.css(cfg["content_selector"]):
                for junk in node.css("script, style, noscript, nav, form, svg"):
                    junk.decompose()
                parts.append(node.text(separator="\n"))
            text = "\n".join(parts)
        else:
            text = page_text(r.text, it.url)
        text = _join_spelled(text)
        if len(text) > 150:
            n += _save(cfg["name"], title, it.url, text[:6000])
        if i % 10 == 0:
            log(f"  {cfg['name']}: {i}/{len(items)} pages read")
    log(f"{cfg['name']}: {n} new or updated projects")
    return n


def _sections(client: PoliteClient, cfg: dict, log) -> int:
    """Sites that put every project on one page, one element per project
    (e.g. maurobubbico.it: <section id="<slug>">). Each element's id becomes
    the project URL through `url_template`."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(client.get(cfg["url"]).text)
    n = 0
    for node in tree.css(cfg["section_selector"])[: int(cfg.get("max_projects", 60))]:
        slug = node.attributes.get("id") or ""
        url = cfg["url_template"].format(id=slug) if slug else cfg["url"]
        # The section holds only the project, so its plain text is used as is
        # (trafilatura discards short fragments like these).
        lines = (re.sub(r"\s+", " ", ln).strip() for ln in node.text(separator="\n").splitlines())
        text = "\n".join(ln for ln in lines if ln)
        if len(text) > 150:
            n += _save(cfg["name"], slug.replace("-", " ").capitalize() or "project", url, text[:6000])
    log(f"{cfg['name']}: {n} new or updated projects")
    return n


def _wp_api(client: PoliteClient, cfg: dict, log) -> int:
    """WordPress sites whose pages need JavaScript but expose their posts
    through the standard REST API (/wp-json/wp/v2/posts)."""
    import html as html_mod

    base = cfg["url"].rstrip("/")
    n, page, limit = 0, 1, int(cfg.get("max_projects", 60))
    seen = 0
    while seen < limit:
        r = client.get(f"{base}/wp-json/wp/v2/{cfg.get('post_type', 'posts')}", params={"per_page": 50, "page": page})
        if r.status_code >= 400:
            break
        posts = r.json()
        if not posts:
            break
        for post in posts:
            if seen >= limit:
                break
            seen += 1
            def field(name: str) -> str:  # standard {"rendered": ...} or a plain string (custom APIs)
                v = post.get(name) or ""
                return v.get("rendered", "") if isinstance(v, dict) else str(v)
            title = html_mod.unescape(strip_html(field("title"))) or post.get("slug", "")
            text = strip_html(field("content")) or strip_html(field("excerpt"))
            if len(text) > 150:
                n += _save(cfg["name"], title, post.get("link") or f"{base}/{post.get('slug')}", f"{title}\n{text}"[:6000])
        page += 1
    log(f"{cfg['name']}: {n} new or updated projects")
    return n


KINDS = {"fa_api": _fa_api, "html_list": _html_list, "sections": _sections, "wp_api": _wp_api}


def update(log=print, only: list[str] | None = None) -> int:
    """Fetch [[reference_work]] projects; `only` limits it to those studios."""
    if only:
        return _update([c for c in prefs().get("reference_work", []) if c["name"] in only], log)
    return _update(prefs().get("reference_work", []), log)


def _update(studios: list[dict], log) -> int:
    client = PoliteClient()
    total = 0
    try:
        for cfg in studios:
            log(f"Reading reference projects: {cfg['name']}")
            try:
                total += KINDS[cfg.get("kind", "html_list")](client, cfg, log)
            except Exception as e:
                log(f"{cfg['name']}: failed ({type(e).__name__}: {e})")
    finally:
        client.close()
    return total


def load() -> list[dict]:
    out = []
    if not REF_DIR.exists():
        return out
    for path in sorted(REF_DIR.glob("*/*.md")):
        raw = path.read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n?(.*)", raw, re.S)
        if not m:
            continue
        meta = yaml.safe_load(m.group(1)) or {}
        out.append({"id": f"{path.parent.name}/{path.stem}", "studio": meta.get("studio", path.parent.name),
                    "title": meta.get("title", path.stem), "url": meta.get("url", ""), "text": m.group(2).strip(),
                    "path": str(path)})
    return out


def remove(ref_id: str) -> None:
    path = REF_DIR / f"{ref_id}.md"
    if path.exists():
        dest = path.parent / "_removed"
        dest.mkdir(exist_ok=True)
        path.replace(dest / path.name)
