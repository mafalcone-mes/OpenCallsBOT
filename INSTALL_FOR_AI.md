# Installing vacancies_searcher (instructions for an AI coding assistant)

You are installing this app for a person on **their** computer (most likely a
Mac). Follow these steps in order. Explain briefly what you are doing, **ask
before installing system software**, and ask the person the questions in
step 6: never invent facts about them. Everything runs locally and is free.

The app: Python (managed by `uv`) + local AI models served by **Ollama**. It
reads the person's portfolio, searches the sources listed in a **shared Google
Sheet**, and learns their taste locally. Read `README.md` and `CLAUDE.md` first.

**Privacy rule:** `preferences.toml`, `data/`, `output/` and `studios.toml` are
personal and git-ignored. Never commit them, never copy them anywhere else.

---

## 1. Look at the machine

```bash
sw_vers                                   # macOS version (13+ recommended)
uname -m                                  # arm64 = Apple Silicon, x86_64 = Intel
sysctl -n hw.memsize | awk '{print $1/1073741824 " GB"}'   # total RAM
df -h ~ | tail -1                         # free disk: need ~12 GB for the models
```

Pick the models from this table and remember the choice for step 5:

| Machine | Chat model (`[llm].model`) | Fallback | Embedding model | `num_ctx` | Notes |
|---|---|---|---|---|---|
| Apple Silicon, 32 GB+ | `qwen3.5:9b` | `qwen3:8b` | `bge-m3` | 8192 | Comfortable |
| Apple Silicon, 16-24 GB | `qwen3.5:9b` | `qwen3:8b` | `bge-m3` | 6144 | Close heavy apps during a refresh |
| Apple Silicon, 8 GB | `qwen3:4b` | `qwen3:1.7b` | `qwen3-embedding:0.6b` | 4096 | Slower, less precise reading of calls |
| Intel Mac (any RAM) | `qwen3:4b` | `qwen3:1.7b` | `qwen3-embedding:0.6b` | 4096 | No GPU: very slow. Tell the person; suggest refreshing a few sources at a time |

If a model name is no longer available in Ollama's library, choose the closest
current multilingual model of the same size that supports JSON-schema output,
and tell the person.

## 2. System prerequisites (ask before installing each)

```bash
brew --version || echo "no Homebrew"      # https://brew.sh if missing (ask first)
git --version
uv --version || brew install uv           # Python + dependencies manager (it installs Python 3.13 itself)
ollama --version || brew install --cask ollama   # or the app from https://ollama.com/download
```

Optional:
- **Google Chrome** (or Edge/Chromium/Brave): used to print briefs as PDF.
  `ls "/Applications/Google Chrome.app"` to check.
- **Node.js 22+** only if the portfolio is a website in the `website` format
  (step 6a): `node --version || brew install node`.

Start Ollama (the app icon in the menu bar, or `open -a Ollama`), then:
`curl -s http://127.0.0.1:11434/api/version` must answer.

## 3. Get the code

The repository is private: the person needs to have accepted the GitHub
invitation from its owner.

```bash
gh --version || brew install gh
gh auth status || gh auth login           # the person logs in in the browser
cd ~ && gh repo clone mafalcone-mes/OpenCallsBOT && cd OpenCallsBOT
```

(Or `git clone https://github.com/mafalcone-mes/OpenCallsBOT.git`.)

## 4. Python environment and tests

```bash
uv sync                                   # creates .venv with Python 3.13 and all dependencies
uv run pytest -q                          # must pass; the website-format tests are skipped without a website
```

`python-jobspy` pins an old numpy; `pyproject.toml` overrides it on purpose
(`[tool.uv] override-dependencies`). If `uv sync` fails, report the error, don't
loosen other pins at random.

## 5. Download the models (from the table in step 1)

```bash
ollama pull qwen3.5:9b      # chat model (~6.6 GB); replace with your table choice
ollama pull qwen3:8b        # fallback
ollama pull bge-m3          # embeddings (~1.2 GB)
ollama list
```

## 6. Interview the person, then write `preferences.toml`

```bash
cp preferences.example.toml preferences.toml
```

Ask these questions (in their language), one group at a time. Fill
`preferences.toml` with the answers; the comments in the file explain each key.

**a. Their portfolio** (`[portfolio]`) - ask which form it takes:
1. **A folder of project texts** (recommended; also the right choice if they
   have no website): copy `portfolio-template/` to a place of theirs, e.g.
   `~/my-portfolio`, and help them fill `profile.md` and one
   `projects/<name>.md` per project. You may draft these **from material they
   give you** (their website pages, an existing PDF, texts they paste), then
   ask them to check every file; never invent projects, dates or roles. Set
   `format = "folder"` and `dir` to that folder (absolute path is fine).
2. **An existing PDF portfolio/CV**: put the PDF(s) in a folder (CV as
   `cv.pdf`), set `format = "pdf"`. Explain that matching is less precise than
   with the folder format, and offer to build the folder from the PDF later.
3. **A portfolio website in this app's `website` format** (a Next.js site with
   `content/projects.csv`, `content/categories.ts`, `lib/site.ts`): set
   `format = "website"`, `dir` = the site folder; needs Node and `npm install`
   in that folder. Only this format produces tailored portfolio PDFs.

Then check: `uv run vs portfolio` lists their projects and any warnings.

**b. Facts for eligibility** (`[me]`), all optional, never shared:
citizenships (ISO codes), country of residence (empty if moving/unsure), birth
year (some calls have age limits), degrees with years, languages they would
apply in with level, seniority (junior / mid-level / senior) and what they are
open to (internships, freelance, residencies...).

**c. Taste** (`[taste]`):
- the roles and fields they want (these become `keywords_plus`; write them in
  every language they read, e.g. "graphic designer", "grafico", "grafisch
  ontwerper"; use `*` for stems like `fotograf*`);
- what puts them off (`keywords_minus`, e.g. "senior", "5+ years", "sales");
- 2-5 studios/people whose work they admire (`reference_studios`; optionally
  also as `[[reference_work]]` with their projects page: check the page and its
  link pattern live before adding, then `uv run vs references`);
- adjacent fields worth exploring (`adjacent_fields`).

**d. Job-board searches** (`[search]`): countries (Indeed names, e.g.
"netherlands", "italy") and job titles in English (`queries`) and per country
in the local language (`[search.local]`).

**e. Models** (`[llm]`): the choice from step 1.

**f. Weights:** keep the defaults. They adapt as the person stars and dismiss.

**g. Sources:** nothing to do. `[sheet].url` already points to the shared
sheet; the app reads it. If they want to add sources to the shared list, the
sheet's owner gives them edit access on Google. Rows: `kind` = `source` (needs
technical settings: ask the owner or an AI assistant to configure them),
`watch` (a page checked for new calls: name + url is enough), `studio`
(studio to approach: name, url, city, country, studio_types) or `excluded`.

Validate: `uv run python -c "import tomllib; tomllib.load(open('preferences.toml','rb')); print('ok')"`.

## 7. First run

```bash
chmod +x start.sh
./start.sh                                # starts Ollama if needed, opens http://127.0.0.1:8765
```

In the dashboard: **Sources** must show the sheet's sources (if a "Sources
sheet" notice says it can't be read, the snapshot copy is used: still fine).
Then press **Refresh**. The first refresh reads a few hundred pages with the
local model: about an hour on a 16 GB Apple Silicon Mac, longer on smaller
machines. Keep the Mac awake and plugged in, close heavy apps. From the
terminal instead: `caffeinate -i uv run vs refresh`.

## 8. Show the person how to use it

- **★** what they like, **Dismiss** with a reason what they don't; after 15-20
  signals the ranking improves (**Taste → Is it learning?** measures it).
- **Taste → Add an example**: calls they found elsewhere (paste the link or the
  text). The **bookmarklet** on that page does it in one click from the browser.
- Open an opportunity → **Prepare brief**: projects to present, checklist,
  deadlines (.ics).
- **Studios**: studios to approach with spontaneous applications.
- Nothing runs in the background: they press Refresh when they want new results.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| "Ollama is not reachable" | Open the Ollama app; `curl http://127.0.0.1:11434/api/version` |
| "None of the configured chat models are pulled" | `ollama pull <model in [llm]>` |
| Refresh very slow or the Mac swaps | Smaller model / lower `num_ctx` (table in step 1); close apps; refresh one source at a time: `uv run vs refresh --source <id>` |
| "preferences.toml not found" | Step 6 |
| Portfolio error at start | `uv run vs portfolio` shows what is wrong; check `[portfolio].format` and `dir` |
| "Sources sheet: could not read" | The app uses its last copy (`data/sheet-cache.csv`) or `sources-snapshot.csv`; check the internet connection and that the sheet is shared "anyone with the link" |
| Brief PDF fails | Needs Chrome/Edge/Chromium; the tailored *portfolio* PDF only exists for the website format |
| A source shows an error in **Sources** | Sites change or block bots; tell the sheet's owner (it is shared) |
| Port 8765 busy | `uv run vs serve --port 8766` |

## 10. Updating later

```bash
git pull && uv sync && uv run pytest -q
```

`preferences.toml` and `data/` are untouched by updates. If
`preferences.example.toml` gained new keys, copy the new keys (with their
defaults) into `preferences.toml`.
