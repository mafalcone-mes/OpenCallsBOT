"""Screenshots, an action inventory and an accessibility check of every page
of the web app, for the UX map. Dev tool only (Playwright + axe-core).

    uv run vs serve --port 8799        # in another terminal
    uv run python scripts/ux_capture.py --base http://127.0.0.1:8799

Read-only: it opens pages, menus and dialogs but never submits a form or sends
an htmx POST, so nothing in your data changes. Output goes to output/ux/
(personal: never committed).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page, sync_playwright

OUT = Path("output/ux")

# GET pages; the first opportunity and chat are found on the fly.
PAGES = [
    ("ask", "/"), ("chats", "/chats"), ("feed", "/feed"), ("feed-new", "/feed?new=1"),
    ("discovery", "/discovery"), ("todo", "/todo"), ("todo-deleted", "/todo?view=deleted"),
    ("archive", "/archive"), ("recap", "/recap"), ("taste", "/taste"), ("sources", "/sources"),
    ("studios", "/studios"), ("hidden", "/hidden"), ("add", "/add"),
]

# States reached by a click that only opens something (no request that changes data).
STATES = {
    "feed": [("more-filters", "details.f-more > summary"), ("dismiss-menu", "details.dismiss > summary")],
    "todo": [("card-menu", "details.menu > summary")],
    "detail": [("dismiss-menu", ".side details.dismiss > summary"), ("raw-call", "details.rawcall > summary")],
}

INVENTORY_JS = """() => [...document.querySelectorAll('main a[href], main button, main select, main input:not([type=hidden]), main textarea, main summary, aside.sidebar a, aside.sidebar button')]
  .map(el => {
    const form = el.closest('form');
    return {
      tag: el.tagName.toLowerCase(),
      label: (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
      href: el.getAttribute('href') || '',
      method: el.getAttribute('hx-post') ? 'POST' : el.getAttribute('hx-get') ? 'GET' : form ? (form.method || 'get').toUpperCase() : '',
      action: el.getAttribute('hx-post') || el.getAttribute('hx-get') || el.getAttribute('formaction') || (form ? form.getAttribute('action') || form.getAttribute('hx-post') || '' : ''),
      htmx: !!(el.getAttribute('hx-post') || el.getAttribute('hx-get') || (form && form.getAttribute('hx-post'))),
      area: el.closest('aside.sidebar') ? 'sidebar' : 'main',
    };
  })"""


def no_dialogs(page: Page) -> None:
    """Close the once-a-day modals so they don't cover every screenshot (they are captured on their own)."""
    page.evaluate("() => document.querySelectorAll('dialog[open]').forEach(d => d.close())")


def capture(page: Page, axe: Axe, name: str, url: str, base: str, report: dict) -> None:
    page.goto(base + url, wait_until="networkidle")
    dialogs = page.evaluate("() => [...document.querySelectorAll('dialog[open]')].map(d => d.id)")
    for d in dialogs:  # the modal as it first appears
        page.screenshot(path=OUT / f"{name}--modal-{d}.png")
    no_dialogs(page)
    page.screenshot(path=OUT / f"{name}.png", full_page=True)
    res = axe.run(page)
    violations = [{"id": v["id"], "impact": v["impact"], "help": v["help"], "nodes": len(v["nodes"])}
                  for v in res.response["violations"]]
    entry = {"url": url, "title": page.title(), "modals": dialogs, "actions": page.evaluate(INVENTORY_JS),
             "axe": violations, "states": []}
    for state, sel in STATES.get(name.split("-")[0] if name.startswith("detail") else name, []):
        el = page.locator(sel).first
        if el.count():
            el.click()
            page.wait_for_timeout(250)
            page.screenshot(path=OUT / f"{name}--{state}.png", full_page=False)
            entry["states"].append(state)
            page.goto(base + url, wait_until="networkidle"); no_dialogs(page)
    report[name] = entry
    print(f"{name:14} {len(entry['actions']):3} actions  {len(violations):2} axe rules  {', '.join(entry['states'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        axe = Axe()
        for vp_name, vp in [("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 390, "height": 844})]:
            page = browser.new_page(viewport=vp)
            # Never send a request that changes something, whatever gets clicked.
            page.route("**/*", lambda r: r.abort() if r.request.method != "GET" else r.continue_())
            pages = list(PAGES)
            page.goto(args.base + "/feed", wait_until="networkidle")
            m = re.search(r'href="/o/(\d+)"', page.content())
            if m:
                pages.append(("detail", f"/o/{m.group(1)}"))
            page.goto(args.base + "/chats", wait_until="networkidle")
            m = re.search(r'href="/chat/(\d+)"', page.content())
            if m:
                pages.append(("chat-saved", f"/chat/{m.group(1)}"))
            if vp_name == "mobile":  # the main screens plus the open menu
                pages = [x for x in pages if x[0] in ("ask", "feed", "detail", "todo", "taste")]
            for name, url in pages:
                capture(page, axe, f"{name}" if vp_name == "desktop" else f"{name}-mobile", url, args.base, report)
            if vp_name == "mobile":
                page.goto(args.base + "/feed", wait_until="networkidle"); no_dialogs(page)
                page.click("#sb-open"); page.wait_for_timeout(300)
                page.screenshot(path=OUT / "sidebar-mobile.png")
            page.close()
        browser.close()
    (OUT / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(report)} screens -> {OUT}/")


if __name__ == "__main__":
    main()
