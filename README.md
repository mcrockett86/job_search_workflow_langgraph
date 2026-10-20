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

# 3. Run the job search pipeline
./run.sh            # interactive (prompts for confirmation)
python -m agents.job_finder --skip-prompt   # non-interactive
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