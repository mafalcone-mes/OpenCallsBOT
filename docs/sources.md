# Source audit

Checked on 2026-09-30 with `scripts/audit_sources.py` (robots.txt, feeds, whether the page is server-rendered).
Re-run the script to update this table when adding sources.

| Source | Access | robots.txt | Status in the app |
|---|---|---|---|
| On the Move | RSS at `https://on-the-move.org/feed` (10 latest items; news and calls mixed) | allowed | **On** (`rss`) |
| Res Artis open calls | Server-rendered listing, ~190 call links. Its open-calls RSS answers **403** | allowed | **On** (`html_list`) |
| Residency Unlimited | WordPress RSS `/feed/` (newsletters "E-Blast" mixed in) | allowed | **On** (`rss`, low weight) |
| Exibart, bandi e concorsi | Category RSS `/categoria/bandi-e-concorsi/feed/` | allowed | **On** (`rss`) |
| art a part of cult(ure) | WordPress RSS; mostly art news, a few calls | allowed | **On** (`rss`, low weight; non-calls are dropped by the LLM) |
| EU Funding & Tenders | Anonymous search API (POST, `apiKey=SEDIA`). Its status filter lets closed calls through, so the adapter drops past deadlines | no robots on the API host | **On** (`eu_ft`, tagged "for organisations") |
| EURAXESS | Server-rendered search; links `/jobs/<id>`. **Free-text `keywords` is ignored** without JS, so the adapter uses research-field facets `?f[0]=job_research_field:<id>`. No public read API or RSS (the XML is for uploading) | allowed | **On** (`euraxess`, prefiltered to the 40 closest) |
| Indeed via JobSpy | Library; NL, IT, DE, ES. **LinkedIn excluded by decision**, Glassdoor not used | n/a | **On** (best effort, auto-off after 2 blocked runs) |
| Studio watchlist | Hash diff of each page + new call-like links | per site | **On** (11 pages; URLs to confirm in the Sources view) |
| ReliefWeb jobs (UN OCHA) | RSS with `?search=` per query; each item carries organisation, closing date and the full description. The API v2 needs an approved free `appname` (request at apidoc.reliefweb.int) | allowed | **On** (`rss`, 11 queries, prefiltered to 30) |
| UN Secretariat (careers.un.org) | Public JSON API used by its search page (`POST /api/public/opening/jo/list/filteredV2/en`) | allowed | **On** (`un_careers`, 11 keywords, prefiltered to 25) |
| UNESCO | Listing page `/go/All-jobs-openings/782502/`. Its RSS path is **disallowed** in robots.txt | allowed (listing) | **On** (`html_list`, prefiltered to 15) |
| WHO | Taleo: JSON service `rest/jobboard/searchjobs`; detail text decoded from the page's `initialHistory` field | no robots.txt | **On** (`taleo`, prefiltered to 15) |
| FAO | Taleo, same as WHO | no robots.txt | **On** (`taleo`, prefiltered to 15) |
| UNjobs.org, UNDP jobs | **Disallowed** in robots.txt | — | Off |
| Devex | 403 | — | Off |
| Impactpool | Server-rendered, `/jobs/<id>`. Its terms forbid scraping ("scrape or copy platform data without permission") | allowed | **Manual only**: add its jobs as examples |
| Idealist | Job listings are JS-rendered (no job links in the raw HTML) | allowed | Off |
| EU EPSO | Server-rendered listings | allowed | Later (check terms) |
| Fontanel Jobs (NL) | Server-rendered listing, `/jobs/<slug>` | allowed | **On** (`html_list`) |
| Magnet.me (NL) | Listings for design jobs, design internships and marketing-communication, `/en/opportunity/<id>/<slug>` | allowed | **On** (`html_list`, 3 pages) |
| UIUXJobsBoard | One listing per country (NL, IT, BE, DE, ES, PT, FR, remote), `/job/<id>-<slug>` | allowed | **On** (`html_list`, 8 pages) |
| Kunstenbond vacatures (NL) | Arts-and-culture union job page, `/vacature-<slug>/` | allowed | **On** (`html_list`) |
| Dribbble Jobs | RSS `jobs.rss` (mostly US and remote) | allowed | **On** (`rss`, outside Europe ranks lower) |
| If You Could Jobs (UK/EU) | Server-rendered homepage with ~90 job cards, `/jobs/<id>`; each card states the level. No scraping clause in its terms (checked 30 Sep 2026) | no robots.txt | **On** (`html_list`, prefiltered to 20) |
| Creative Lives in Progress (UK) | Server-rendered `/opportunities`: internships, junior jobs, open calls, workshops. No scraping clause in its terms | allowed | **On** (`html_list`, prefiltered to 15) |
| We Work Remotely | RSS per category, `remote-design-jobs.rss` (~20 items, full description). Mostly senior/US | allowed | **On** (`rss`, low weight) |
| La Scuola Open Source (Bari) | Server-rendered `/it/attivita` (~18 workshops and events, past ones dropped by the deadline check); `/it/proponi` on the watchlist; its `/it/archivio` projects are reference work | allowed | **On** (`html_list`, weight 1.0) |
| Baltan Laboratories (Eindhoven) | Server-rendered homepage lists the whole library; only call-like links (open call, call for, vacancy, internship, residency, fellowship, workshop events) are read, the 20 newest. Its other library pages are reference work | no robots.txt | **On** (`html_list`, weight 1.0) |
| Working Nomads | Public JSON API `/api/exposed_jobs/`, but only ~1 design job in 56 per month | allowed | Off (too little design) |
| The Dots, Academic Positions, Data Visualization Society, ArtsProfessional | **403** | — | Off |
| Wellfound | Server-rendered; US startups, mostly mid/senior | allowed | Off (not your market for now) |
| Krop, Authentic Jobs | No job listing in the raw HTML (JS or gone) | — | Off |
| jobs.ac.uk | Server-rendered search pages, no public feed found | allowed | Later (UK academia) |
| e-flux / art&education jobs | `/jobs` returns 404 | — | Off |
| Werken voor Nederland | JS-rendered results | allowed | Later |
| Behance, Creativepool, Coroflot, Working Not Working, designjobsboard | **403** | — | Off |
| It's Nice That jobs | Page gone (404) | — | Off |
| InfoJobs Italia | JS-rendered | — | Off (Italy is covered by Indeed with Italian job titles) |
| TransArtists | Since 1 Oct 2026 the home page and `/en/transartists-calls` are readable (calls at `/en/news/<slug>`); `/en/other-open-calls` still answers **403** | no robots.txt | **On** (`html_list`, 2 pages, max 25) |
| AFIELD | WordPress RSS `/feed/` (99 posts: calls mixed with stories, filtered with `require`) | allowed | **On** (`rss`) |
| Picter (submit.picter.com) | **429** from the first request (bot protection), JS app | — | Off: add its calls as examples (link or pasted text, or the bookmarklet) |
| GIJN jobs | **403** | — | Off |
| Dezeen Jobs | RSS answers **403** | allowed | Off |
| Italian Council (MiC) | RSS exists but is empty | allowed | Watchlist instead |
| Mostrami | No usable feed (the "bandi" feed is a comments feed; the page is a seasonal article) | allowed | Off |
| giovaniartisti.it | `/bandi` returns 404 | allowed | Off (find the right URL) |
| AcademicTransfer | JS-rendered (36 links in raw HTML); no RSS | allowed | Later (needs Crawl4AI or its JSON endpoint) |
| Creative Industries Fund NL | JS-rendered | allowed | Later (watchlist) |
| Mondriaan Fonds | Server-rendered, no feed | no robots.txt | Later (watchlist) |
| ArtConnect | Server-rendered listing | allowed | Later: check its terms first |
| Fontanel Jobs, UIUXJobsBoard | Server-rendered, no feed | allowed | Later (`html_list`) |
| e-flux announcements | Server-rendered, no feed; institutional news more than calls | allowed | Later, for discovering institutions |

## Added 1 Oct 2026 (discovery from each organisation's own home page: `scripts/discover_sources.py`, raw output in `docs/discovery.txt`)

| Family | Read as listings / feeds | Watched pages |
|---|---|---|
| Visual journalism | ProPublica jobs (Greenhouse), EJC funding, Villamedia vacatures + opdrachten (RSS); Indeed searches for data/visual journalist, graphics editor in 6 languages | Journalismfund Europe grants, ProPublica fellowships |
| Investigative / spatial | Border Forensics, Airwars (feeds, call-like posts only) | SITU opportunities |
| Art + science + tech | S+T+ARTS (feed), Triennale jobs | Ars Electronica open calls, Prix, careers; Akademie Schloss Solitude fellowship + jobs; Cittadellarte UNIDEE; MAXXI open calls + internships |
| Funding for individuals | European Cultural Foundation open calls | GAI Giovani Artisti Italiani, Prince Claus Fund awards |
| Competitions | ADI (call-like posts) | Malofiej, Information+, D&AD New Blood |
| Research / design schools | Sandberg news (call-like) | DensityDesign opportunities, DAE Community Grant + Residency Guide |
| Puglia | — | Puglia Creativa opportunità |

**Not possible:** The Pudding (no calls page), Forensis (forensis.org now redirects to an unrelated domain), Earshot and V2_ and Teatro Pubblico Pugliese (JavaScript-only; V2_'s RSS is malformed), Goldsmiths Centre for Research Architecture (page gone, 404), Culture Moves Europe on goethe.de (403), ADC Young Ones / One Club (robots.txt disallows), IUAV (JavaScript), ISIA Urbino (invalid SSL certificate), KABK (only study admissions), Regione Puglia PIN (pin.regione.puglia.it does not resolve).

**unibz (Free University of Bozen-Bolzano), 1 Oct 2026:** positions search (`/en/home/position-calls/...`) renders results with JavaScript behind Imperva bot protection: not read. Watched instead: the PhD in Experimental Research through Design, Art and Technologies, and the Italian "incarichi" page (collaborator registers). unibz research positions also reach EURAXESS.

**Human rights & civil society (1 Oct 2026):** Human Rights Watch via its Greenhouse public job feed (its own site answers 403); OCCRP, WITNESS, Access Now via BambooHR's public careers list (`/careers/list`, `/careers/<id>/detail`). Watched pages: Global Witness, ICIJ, Forbidden Stories, Tactical Tech, Privacy International, EDRi, Statewatch, Transparency International, Oxfam, MSF, Sea-Watch, Amnesty NL, Bits of Freedom, Oxfam Novib, Amnesty Italia, Emergency, ActionAid Italia, CIVICUS. **Not possible:** Amnesty International (global site, 403), Article 19 (403), Free Press Unlimited (403), Mnemonic (JavaScript-only); Greenpeace International, HRDAG, Openpolis link no jobs page from their home page.
