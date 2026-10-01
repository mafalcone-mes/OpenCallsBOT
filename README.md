# OpenCallsBOT

_vacancies_searcher: scraping websites to look for open calls, jobs and residencies that fit your work._
A local tool for creative professionals: it finds **jobs, residencies, open
calls, grants, fellowships, competitions and research positions** that fit your
portfolio, **learns your taste** from what you star and dismiss, and prepares an
**application brief** for each opportunity (which projects to present and why,
a checklist of what to send, deadlines as calendar events).

- **Local and free.** It runs on your computer with open-source AI models
  (Ollama). No accounts, no paid APIs, no subscriptions.
- **Your portfolio is the source of truth.** It reads your portfolio (a folder
  of project texts, a PDF, or a portfolio website) every time; edit your
  portfolio and the matching follows.
- **Shared sources.** The list of sites it searches (about 50 sources and 80
  watched pages: design job boards, residencies, open calls, UN agencies, NGOs,
  photography festivals, studios...) lives in a shared Google Sheet.
- **Private taste.** Your stars, examples and preferences stay on your machine.

## Install

Give `INSTALL_FOR_AI.md` to an AI coding assistant (e.g. Claude Code) on your
machine: it installs everything, asks you the questions it needs, and checks
that it works. Doing it by hand: follow the same file.

## Use

- Start: `./start.sh` (macOS/Linux) or `start.bat` (Windows), then open
  http://127.0.0.1:8765.
- **Refresh** searches the sources (the first run takes about an hour; later
  ones only read what's new).
- ★ what you like and **Dismiss** (with a reason) what you don't: after 15-20
  signals the ranking clearly improves (check it in **Taste → Is it learning?**).
- Open an opportunity and **Prepare brief** for the application checklist and
  the projects to show.
- **Studios** lists studios to approach with spontaneous applications.

## Commands

| Command | What it does |
|---|---|
| `uv run vs serve` | Dashboard |
| `uv run vs refresh [--force] [--source ID]` | Search the sources, read new calls with the local model, score |
| `uv run vs rescore` | Score again (after editing your preferences or adding examples) |
| `uv run vs brief <id>` | Application brief for opportunity `<id>` |
| `uv run vs example interested --url URL --why "..."` | Add an example you found yourself |
| `uv run vs references` | Read the projects of studios you admire (`[[reference_work]]`) |
| `uv run vs eval` | Check that taste learning improves the ranking |
| `uv run vs portfolio` | Show what is read from your portfolio |
| `uv run pytest` | Tests (offline) |

## Files

| Path | What | Shared? |
|---|---|---|
| `preferences.toml` | Your facts (for eligibility), taste, keywords, weights, portfolio location | **No** (template: `preferences.example.toml`) |
| `data/` | Everything learned about you: examples, stars, database, reference projects | **No** |
| `output/` | Briefs, PDFs, reports | **No** |
| Google Sheet (`[sheet].url`) | Sources, watched pages, studios, excluded sites | Yes |
| `sources-snapshot.csv` | Copy of the sheet used if it can't be reached | Yes |
| `portfolio-template/` | Template for the folder portfolio format | Yes |
| `docs/sources.md` | Notes on how each source is read and why some are excluded | Yes |
