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
(`cache/jobs.db`, auto-merged from the legacy CSVs on first start):

- **▶ Run new scan** — triggers the scrape→screen pipeline with a live log.
- **Browse & filter** — status badges, company/type dropdowns, free-text
  search, min-salary, sorting.
- **Hover preview** — hovering a card fetches + summarizes the listing once
  (cached in the DB); pipeline runs also store a summary at screen time.
- **Click-through drawer** — prev/next (`j`/`k`, arrows), summary, full text,
  open the original URL.
- **Export** — CSV or Markdown of the filtered set, summaries included.
- **⚙ Search settings** — tailor the scan to *you* without touching code:
  - **Resume PDF** — upload the resume used for job-vs-resume matching
    (validated: real PDF, selectable text, ≤10 MB). Overwrites the cached copy.
  - **Job-search URLs** — paste/build your own search-results URLs, one per
    line. **Save** overwrites the previous list.
  - **Reset** buttons restore the built-in defaults (the bundled software
    resume and the curated LinkedIn queries).
  Saved values are cached in local files under `user_config/`
  (`resume.pdf`, `search_urls.txt` — git-ignored) and take effect on the next
  scan. With no `user_config/` files the app uses the bundled defaults.

The server binds `127.0.0.1` only — it can launch a run that uses your
LinkedIn credentials, so keep it off the network. Override with
`JOB_VIEWER_HOST`/`JOB_VIEWER_PORT` if you know what you're doing.

```bash
uv run pytest tests -q   # test suite (db, summarizer, API)
```

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
5. **Output** — Results are written to `results_output.csv`. Prior-job cache is updated at `cache/target_jobs_old.csv`.

## LangGraph DAG

![Diagram Agent Flow](http://raw.githubusercontent.com/mcrockett86/job_search_langgraph_mcp/refs/heads/master/job_graph.png?raw=true)