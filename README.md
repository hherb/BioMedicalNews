# BioMedical News

A biomedical news reader that fetches preprints from medRxiv, bioRxiv, and Europe PMC, uses LLM-based assessment for relevance scoring and quality evaluation, and delivers curated digests via email.

Built on [bmlib](https://github.com/hherb/bmlib) — a shared library for LLM abstraction, quality assessment, transparency analysis, and database utilities.

## Features

- **Multi-source fetching** — medRxiv, bioRxiv, and Europe PMC preprint servers
- **LLM-based relevance scoring** — structured JSON responses with relevance scores and summaries
- **Quality assessment** — 3-tier pipeline (metadata → LLM classifier → deep assessment) via bmlib
- **Transparency analysis** — multi-API bias detection (optional, via bmlib)
- **Configurable prompt templates** — Jinja2 templates with user overrides
- **Email digests** — HTML + plain-text emails with scored paper summaries and DOI links
- **Database abstraction** — SQLite (default) or PostgreSQL, pure-function DB layer via bmlib
- **CLI interface** — fetch, score, search, and send digests from the command line

## Quick start

```bash
# Install
uv pip install -e ".[dev]"

# Initialise database and config
bmnews init

# Edit your preferences
nano ~/.bmnews/config.toml

# Run the full pipeline
bmnews run

# Or run individual steps
bmnews fetch --days 7
bmnews score
bmnews digest
bmnews search "machine learning"
```

## Configuration

Run `bmnews init` to generate `~/.bmnews/config.toml`.

Key sections:

| Section | Purpose |
|---------|---------|
| `[database]` | Backend (`sqlite` / `postgresql`), connection params |
| `[sources]` | Enable/disable medRxiv, bioRxiv, Europe PMC; lookback window |
| `[llm]` | Provider (`ollama` / `anthropic`), model, concurrency |
| `[scoring]` | Relevance and combined score thresholds |
| `[quality]` | Quality assessment tier (1–3), minimum quality tier |
| `[transparency]` | Enable/disable, score threshold for analysis |
| `[user]` | Name, email, research interests |
| `[email]` | SMTP server settings for digest delivery |

## Architecture

```
bmnews/
  config.py          # TOML config loading
  cli.py             # Click CLI commands
  pipeline.py        # Orchestrates fetch → store → score → digest → deliver
  db/
    schema.py        # DDL for SQLite and PostgreSQL (all SQL lives here)
    operations.py    # Pure-function CRUD via bmlib.db
  fetchers/
    base.py          # FetchedPaper dataclass
    medrxiv.py       # medRxiv / bioRxiv API client
    europepmc.py     # Europe PMC REST API client
  scoring/
    relevance_agent.py  # LLM-based relevance scoring (BaseAgent subclass)
    scorer.py           # Orchestrates relevance + quality scoring
  digest/
    renderer.py      # Jinja2 HTML + plain-text rendering
    sender.py        # SMTP email delivery
templates/
  relevance_system.txt    # LLM system prompt
  relevance_scoring.txt   # LLM scoring prompt (Jinja2)
  digest_email.html       # HTML digest template
  digest_text.txt         # Plain-text digest template
tests/
```

## Dependencies

- **bmlib** — LLM abstraction, DB utilities, quality/transparency assessment
- **httpx** — HTTP client for fetching papers
- **click** — CLI framework
- **jinja2** — Template engine (prompt templates + email rendering)

## License

AGPL-3.0 — see [LICENSE](LICENSE).
