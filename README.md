# job_search_langgraph
Job Search Agent Workflow Using LangGraph.  Scrapes available jobs from LinkedIn, gathers details of each job, and compares for match with content available from user-provided resume in PDF format as an MCP resource.  If good match, LangGraph Agentic pipeline generates a customized resume and cover letter for the specific job.  At the end of the workflow, a CSV file is presented containing a summary of the results, with links useful for human-in-the-loop application of the customized application content.  The software keeps a cache of prior jobs screened, so subsequent runs do not duplicate prior work.

## Project Structure

```
agents/
├── __init__.py              # Package marker
├── config.py                # Central configuration (env, LLM singleton, constants)
├── graph.py                 # State definitions + LangGraph construction
├── nodes.py                 # Graph node implementations (read, classify, extract)
├── job_finder.py            # Pipeline entry point (scrape → filter → run graph)
└── linkedin_scraper.py      # Selenium-based LinkedIn URL scraper
tools/
├── __init__.py              # Package marker
├── resume.py                # PDF resume text extraction
└── scraper.py               # HTTP fetcher + HTML-to-text converter

.env.example                 # Template for required environment variables
main.py                      # Top-level entry point (runs pipeline)
```


## Install & Run

```bash
# 1. Clone and install dependencies
./install.sh

# 2. Configure credentials
cp .env.example .env   # then fill in your LinkedIn email/password and OpenAI key

# 3a. Interactive viewer (recommended): browse results, filter, hover-preview,
#     export, and trigger new scans from the browser
uv run python -m agents.web      # → http://127.0.0.1:8734

# 3b. Or run the pipeline headless from the CLI
./run.sh                                   # interactive (prompts for confirmation)
python -m agents.job_finder --skip-prompt  # non-interactive
```

## Web viewer (`agents/web.py`)

A local FastAPI app + single-page viewer over a SQLite job database
(`cache/profiles/<profile>/jobs.db`, per profile):

- **👤 Profile selector** — each profile keeps its own resume, search URLs,
  and discovered-jobs history, so scans never cross-contaminate. Ships with
  **mike** (default; inherited the original global settings + history on
  first run) and **jen** (clean slate). **＋ profile** adds more; the
  browser remembers your last pick and every API call carries `?profile=`.
  Data lives in `user_config/profiles/<slug>/` and `cache/profiles/<slug>/`
  (both git-ignored; registry in `user_config/profiles.json`).
- **▶ Run new scan** — triggers the scrape→screen pipeline with a live log,
  for the selected profile.
- **Browse & filter** — status badges, company/type dropdowns, work-mode
  filter (remote / hybrid / in person / unknown), free-text search,
  min-salary, sorting. Work mode is auto-classified from the posting text
  (title, description, hover-preview text) with hybrid > remote > on-site
  precedence; jobs with no classifiable text stay `unknown`.
- **Hover preview** — hovering a card fetches + summarizes the listing once
  (cached in the DB); pipeline runs also store a summary at screen time.
- **Click-through drawer** — prev/next (`j`/`k`, arrows), summary, full text,
  open the original URL.
- **Live progress bar** — while a scan runs, the header shows a bar with
  counts: pages scraped per search URL (`Scraping LinkedIn: 7/20`) and jobs
  screened (`Screening jobs: 12/15`). It survives a page reload mid-run.
- **Export** — CSV or Markdown of the filtered set (filters included),
  summaries and work mode included.
- **⚙ Search settings** — tailor the scan for the *selected profile*
  without touching code:
  - **Resume PDF** — upload the resume used for job-vs-resume matching
    (validated: real PDF, selectable text, ≤10 MB). Overwrites the cached copy.
  - **Job-search URLs** — paste/build your own search-results URLs, one per
    line. **Save** overwrites the previous list.
  - **Reset** buttons restore the built-in defaults (the bundled software
    resume and the curated LinkedIn queries) for that profile only.
  With no profile files saved, the app uses the bundled defaults.

The server binds `127.0.0.1` only — it can launch a run that uses your
LinkedIn credentials, so keep it off the network. Override with
`JOB_VIEWER_HOST`/`JOB_VIEWER_PORT` if you know what you're doing.

```bash
uv run pytest tests -q   # test suite (db, summarizer, API)
```

## Deploying on the VPS (hostinger stack)

The stack repo (`~/code/hostinger`) ships this app as the `job_search`
service behind `job-search.blueskyengineering.net`:

- **Image**: `containers/Dockerfile_JobSearch` — python:3.13-slim +
  chromium/chromedriver so the "Run new scan" LinkedIn scrape works
  headless in-container. `.dockerignore` keeps `.env`/`dotenv.env` and
  personal resume backups out of the image; credentials arrive only via
  compose environment.
- **Gatekeeper auth**: Caddy `basic_auth` over the WHOLE site, from
  `JOB_SEARCH_USER` + `JOB_SEARCH_BASIC_AUTH_HASH` in the stack `.env`
  (bcrypt hash: `docker compose exec caddy caddy hash-password --plaintext '<pass>'`).
  Fail-closed: missing vars fall back to a throwaway-hash default;
  set-but-empty lines abort `deploy.sh` before the VPS is touched.
- **No published port** — Caddy proxies `job_search:7867` on the compose
  network, so the app is never reachable unauthenticated.
- **State**: named volumes keep `cache/` (per-profile job DBs + ledgers) and
  `user_config/` (profile registry, uploaded resumes + saved search URLs)
  across redeploys.
- **Env** (see hostinger `.env.example`): `JOB_SEARCH_USER`,
  `JOB_SEARCH_BASIC_AUTH_HASH`, `JOB_SEARCH_LINKEDIN_EMAIL`,
  `JOB_SEARCH_LINKEDIN_PASSWORD` (optional — empty disables the Run
  button), plus the shared `OPENAI_API_KEY`.
- **DNS**: needs an `A` record `job-search` → the VPS IP before Caddy can
  issue the certificate.

## LinkedIn login — passwordless (recommended)

Click **🔑 LinkedIn sign-in** in the viewer toolbar. A real LinkedIn page
opens on the server; the modal shows it live and you enter your details —
they are typed into LinkedIn's own form and never stored, logged, or sent
anywhere else. Only the resulting session cookies persist, **per search
profile** (`cache/linkedin_profile/<profile>/`), so mike's LinkedIn account
is never used for jen's scans. Every later "▶ Run new scan" reuses that
session with no password involved. Works on the headless VPS as-is.

Same thing from the CLI (opens a visible browser on your machine):

```bash
uv run python -m agents.linkedin_login           # sign in for the active profile
uv run python -m agents.linkedin_login --status  # check the saved session
uv run python -m agents.linkedin_login --reset   # wipe it
```

`LINKEDIN_PROFILE_DIR` sets the base directory for those per-profile
sessions (default `cache/linkedin_profile`); on the VPS it lives inside the
`job_search_cache` volume, so sessions survive redeploys.

### Legacy: password env vars

`LINKEDIN_EMAIL` + `LINKEDIN_PASSWORD` still work (automated fill), but
prefer the profile — no password in any `.env`.

## Configuration

Copy `.env.example` to `.env` and set:

| Variable | Required | Description |
|----------|----------|-------------|
| `LINKEDIN_EMAIL` | Yes | LinkedIn account email for scraping |
| `LINKEDIN_PASSWORD` | Yes | LinkedIn account password |
| `OPENAI_API_KEY` | Yes | OpenAI API key for LLM classification/extraction |

## How It Works

1. **Scrape** — Selenium logs into LinkedIn and extracts job URLs from search/collection pages (paginated).
2. **Filter** — New URLs are compared against a cache of previously processed jobs to avoid duplicates.
3. **Classify** — For each new job, the graph fetches its HTML content and uses GPT-4o-mini to determine if it matches the candidate's resume.
4. **Extract** — Matching jobs have their metadata (company, title, salary) extracted into structured JSON via LLM.
5. **Output** — Results are written to the active profile's cache dir
   (`cache/profiles/<profile>/results_output.csv`); that profile's
   prior-job cache (`target_jobs_old.csv`) is updated so its next run
   skips these jobs. Other profiles are unaffected.

## LangGraph DAG

![Diagram Agent Flow](http://raw.githubusercontent.com/mcrockett86/job_search_langgraph_mcp/refs/heads/master/job_graph.png?raw=true)