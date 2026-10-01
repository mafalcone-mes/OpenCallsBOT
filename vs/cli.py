"""Command line: `uv run vs <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="vs", description="Find opportunities that fit your portfolio.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Open the dashboard")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    r = sub.add_parser("refresh", help="Fetch sources, extract, score")
    r.add_argument("--force", action="store_true", help="Ignore the per-source minimum interval")
    r.add_argument("--source", help="Only this source id (e.g. resartis, watch:Fabrica)")
    r.add_argument("--no-rerank", action="store_true", help="Skip the LLM explanations (faster)")

    sub.add_parser("rescore", help="Score again without fetching (after editing preferences or examples)")
    b = sub.add_parser("brief", help="Prepare an application brief")
    b.add_argument("id", type=int)
    b.add_argument("--no-pdf", action="store_true")
    sub.add_parser("eval", help="Check whether taste learning improves the ranking")
    sub.add_parser("references", help="Read the projects of studios you admire ([[reference_work]])")
    sub.add_parser("portfolio", help="Show what the app reads from the website")
    sub.add_parser("rebuild", help="Rebuild user state (favourites, statuses, notes) from data/signals.jsonl")
    e = sub.add_parser("example", help="Add an example")
    e.add_argument("label", choices=["interested", "not_interested"])
    e.add_argument("--url", default="")
    e.add_argument("--text-file", help="File with the call's text")
    e.add_argument("--why", default="")

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        uvicorn.run("vs.web.app:app", host="127.0.0.1", port=args.port, log_level="warning")
    elif args.cmd == "refresh":
        from vs.pipeline import refresh
        print(json.dumps(refresh(force=args.force, only=args.source, rerank=not args.no_rerank), indent=2))
    elif args.cmd == "rescore":
        from vs.pipeline import rescore
        print(json.dumps(rescore(), indent=2))
    elif args.cmd == "brief":
        from vs.brief import generate
        print(json.dumps(generate(args.id, make_pdf=not args.no_pdf), indent=2, ensure_ascii=False))
    elif args.cmd == "eval":
        from vs.evaluate import evaluate
        print(json.dumps(evaluate(), indent=2))
    elif args.cmd == "references":
        from vs.references import update
        print(f"{update()} new or updated reference projects")
    elif args.cmd == "portfolio":
        from vs.portfolio import load
        p = load()
        print(f"Website: {p.root}\nHash: {p.profile_hash}\nCategories: {', '.join(c.id for c in p.categories)}")
        for a in p.angles:
            print(f"  {a.key:40} {a.image_count:>2} images  {a.subtitle[:50]}")
        print("Warnings:\n  " + "\n  ".join(p.warnings) if p.warnings else "No warnings")
    elif args.cmd == "rebuild":
        from vs.examples import rebuild_user_state
        print(f"Replayed {rebuild_user_state()} events")
    elif args.cmd == "example":
        from pathlib import Path

        from vs.pipeline import add_example
        text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else ""
        print(json.dumps(add_example(args.label, url=args.url, text=text, why=args.why), indent=2))


if __name__ == "__main__":
    main()
