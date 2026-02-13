# BioMedical News — Developer Manual

This guide is for developers who want to understand, modify, or extend bmnews.

## Project overview

bmnews is a biomedical preprint aggregator with LLM-based relevance scoring. It fetches papers from medRxiv, bioRxiv, and Europe PMC, scores them using an LLM, assesses methodological quality, and delivers ranked digests.

- **Language:** Python 3.11+
- **License:** AGPL-3.0-or-later
- **Author:** Dr. Horst Herb
- **Version:** 0.1.0

## Design philosophy

- **Pure functions over classes** — database operations are stateless functions that take a connection as the first argument, not methods on ORM objects
- **Separation of concerns** — fetch, store, score, and digest are independent pipeline stages that can run separately
- **Configuration-driven** — behavior is controlled by TOML config, not hardcoded values
- **Template-driven** — all LLM prompts and digest output use Jinja2 templates that users can override
- **bmlib as foundation** — shared infrastructure (LLM abstraction, DB utilities, quality assessment, agents) lives in [bmlib](https://github.com/hherb/bmlib), keeping bmnews focused on domain logic

## Documentation

| Guide | Description |
|-------|-------------|
| [Architecture](architecture.md) | System architecture, pipeline flow, design decisions |
| [Codebase](codebase.md) | Module-by-module walkthrough |
| [bmlib Integration](bmlib-integration.md) | How bmnews uses bmlib, extending both projects |
| [Database](database.md) | Schema, operations, backend abstraction |
| [Testing](testing.md) | Running tests, writing tests, test patterns |
| [Contributing](contributing.md) | Code style, conventions, how to add features |

## Quick orientation

```
bmnews/
  cli.py              # Click CLI — entry point
  config.py            # TOML config loading → AppConfig dataclass
  pipeline.py          # Orchestrates: fetch → store → score → digest
  db/
    schema.py          # DDL for SQLite/PostgreSQL
    operations.py      # Pure-function CRUD (all SQL lives here)
  fetchers/
    base.py            # FetchedPaper dataclass
    medrxiv.py         # medRxiv / bioRxiv API client
    europepmc.py       # Europe PMC REST API client
  scoring/
    relevance_agent.py # LLM-based relevance scoring (BaseAgent subclass)
    scorer.py          # Orchestrates relevance + quality scoring
  digest/
    renderer.py        # Jinja2 rendering (HTML + text)
    sender.py          # SMTP email delivery
templates/               # Built-in Jinja2 templates
tests/                   # pytest test suite
```

## Getting started

```bash
git clone https://github.com/hherb/BioMedicalNews.git
cd BioMedicalNews
pip install -e ".[dev]"
pytest
```
