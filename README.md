# BioMedical News

A collection of libraries and apps that process publications on pre-publication servers, assess them for matching user preferences and quality of publication, and presenting the ones that match user preferences in convenient ways.

## Features

- **Multi-source fetching** — medRxiv, bioRxiv, and Europe PMC preprint servers
- **Relevance scoring** — keyword matching (default) or semantic similarity (optional, via sentence-transformers)
- **Quality heuristics** — abstract structure, methodology signals, reporting quality, collaboration indicators
- **Email digests** — HTML + plain-text emails with scored paper summaries and DOI links
- **Database abstraction** — SQLite (default) or PostgreSQL, both with vector embedding support for semantic search
- **CLI interface** — fetch, score, search, and send digests from the command line

## Quick start

```bash
# Install
pip install -e .

# Initialise database and config
bmnews init

# Edit your preferences
# (interests, email settings, scoring thresholds)
nano ~/.bmnews/config.toml

# Run the full pipeline
bmnews run

# Or run individual steps
bmnews fetch --days 7
bmnews score
bmnews digest
bmnews search -q "machine learning"
```

## Configuration

Copy and edit `config.example.toml`, or run `bmnews init` to generate `~/.bmnews/config.toml`.

Key sections:

| Section | Purpose |
|---------|---------|
| `[general]` | Database backend (`sqlite` / `postgresql`), log level |
| `[database.sqlite]` | SQLite file path |
| `[database.postgresql]` | PostgreSQL connection parameters |
| `[sources]` | Enable/disable medRxiv, bioRxiv, Europe PMC; lookback window |
| `[scoring]` | Relevance/quality thresholds; scorer type (`keyword` / `semantic`) |
| `[user]` | Name, email, research interest keywords |
| `[email]` | SMTP server settings for digest delivery |

## Optional dependencies

```bash
# PostgreSQL with pgvector
pip install -e ".[postgresql]"

# Semantic scoring via sentence-transformers
pip install -e ".[semantic]"

# All optional dependencies
pip install -e ".[all]"
```

## Architecture

```
bmnews/
  config.py          # TOML config loading
  cli.py             # Click CLI commands
  pipeline.py        # Orchestrates fetch → score → digest → send
  db/
    engine.py        # SQLAlchemy engine factory (SQLite / PostgreSQL)
    models.py        # ORM models with portable EmbeddingType column
    repository.py    # Data access + vector similarity search
  fetchers/
    base.py          # FetchedPaper dataclass + Fetcher protocol
    medrxiv.py       # medRxiv / bioRxiv API client
    europepmc.py     # Europe PMC REST API client
  scoring/
    relevance.py     # Keyword and semantic relevance scorers
    quality.py       # Heuristic quality scorer
  digest/
    renderer.py      # Jinja2 HTML + plain-text email templates
    sender.py        # SMTP email delivery
tests/
```

## License

AGPL-3.0 — see [LICENSE](LICENSE).
