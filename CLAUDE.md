# vacancies_searcher

A local app (Python + Ollama, zero running cost) that finds jobs, residencies,
open calls, grants and research positions that fit one person's portfolio,
learns their taste from their stars and examples, and prepares application
briefs. Each person runs their own copy with their own preferences; the list
of sources is shared through a Google Sheet.

The app's identity is **Oppo**, a little robot that looks for opportunities.
Colors are tokens at the top of `vs/web/static/app.css` (radar green
`--oppo` for fills, darker `--accent` for text). Oppo's drawings are SVGs in
`vs/web/static/oppo/` (poses and rules in its README), inlined by
`vs/web/oppo.py`; the owner draws them, so never invent the robot.

## Knowledge base: the person's portfolio

The profile and projects the app works from live **outside** this app, in the
person's portfolio. It is the **single source of truth** and its owner edits it
over time (new projects, rewritten texts, new categories).

Three formats are supported, chosen with `[portfolio].format` in
`preferences.toml` (path in `[portfolio].dir` or the `PORTFOLIO_DIR` env var):

| Format | What is read |
|---|---|
| `website` | A Next.js portfolio site: `content/projects.csv` (one row per slug+category, first row = primary category, `#` comments, quoted cells), `content/categories.ts`, `lib/site.ts` (name, role, bio, contacts), `public/projects/<slug>/<category>.md` (write-ups), `public/projects/<slug>/<category>/` (images, `captions.json`), `public/cv.pdf`. Tailored portfolio PDFs come from the site's `scripts/export-portfolio.mjs`. |
| `folder` | `profile.md` (frontmatter + bio), `projects/<slug>.md` (frontmatter: title, year, categories...; body), optional images in `projects/<slug>/`, optional `cv.pdf`. Template: `portfolio-template/`. |
| `pdf` | A folder with the person's portfolio PDF(s) and optional `cv.pdf`; one entry per page with text. |

Rules:

- **Never copy portfolio content into this app** (no hardcoded project lists,
  bio text, categories or skills). Read it from the portfolio every time.
- All reading and parsing is in **one loader module** (`vs/portfolio.py`), so a
  format change only needs a fix there.
- Anything derived from the content (profile summaries, embeddings, keyword
  lists, matching scores) is cached against a **hash of the source files** and
  regenerated when they change. Never treat it as permanent data.
- Categories are data-driven. Never assume a fixed set; a new category must
  work with no code changes.
- The app only **reads** the portfolio. It never writes to it.
- What the portfolio lacks (CV, portfolio PDF, photo) can be uploaded in the
  profile card on Home. Uploads go to `data/profile/` (personal), never into
  the portfolio, and the loader uses them only where the portfolio has
  nothing: the portfolio always wins.

## Sources: the shared Google Sheet

Sources, watched pages, studios to approach and excluded sites are rows in a
Google Sheet (`[sheet].url`), read through its public CSV export at every
refresh, cached in `data/sheet-cache.csv`, with `sources-snapshot.csv` as the
first-run fallback. Code lives in `vs/sheet.py`; `uv run python -m vs.sheet
export` writes all current entries as a CSV to import into the sheet.

Before adding a source: check it live (robots.txt, status, whether it needs
JavaScript, link patterns) starting from the organisation's own pages, never
guessed URLs. Respect robots.txt and bot protections (403/429/checkpoints):
such sites go in the sheet as `excluded` with the reason. No LinkedIn.

## Personal data

Each person trains their own copy. Never commit or share `preferences.toml`,
`data/` (database, examples, stars, signals, reference projects), `output/`
or `studios.toml`. `preferences.example.toml` is the template.

## Running

- Windows: `start.bat`; macOS/Linux: `./start.sh`; or `uv run vs serve`.
- Tests: `uv run pytest` (offline; they use `preferences.example.toml`).
- Installation on a new machine: `INSTALL_FOR_AI.md`.
