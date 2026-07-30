# BioMedical News — Developer Manual

This guide is for developers who want to understand, modify, or extend bmnews.

## Project overview

bmnews is a biomedical news reader with LLM-based relevance scoring. It fetches papers from medRxiv, bioRxiv, Europe PMC, PubMed and OpenAlex, scores them using an LLM, assesses methodological quality, and delivers curated digests and watch notifications by email, Matrix, file, stdout or a desktop GUI.

- **Language:** Python 3.11+
- **License:** AGPL-3.0-or-later
- **Author:** Dr. Horst Herb
- **Version:** 0.3.0

## Design philosophy

- **Pure functions over classes** — database operations are stateless functions that take a connection as the first argument, not methods on ORM objects
- **Separation of concerns** — sync, score, transparency, notify and digest are independent pipeline stages that can run separately
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
  cli.py               # Click CLI — entry point
  config.py            # TOML config loading → AppConfig dataclass
  constants.py         # Fixed behavioural values (not user-tunable)
  metadata.py          # Defensive decoding of the extras blob
  templating.py        # TEMPLATES_DIR + build_template_engine
  pipeline.py          # Orchestrates: sync → score → transparency → notify → digest
  db/
    schema.py          # open_db + init_db (runs migrations; no DDL here)
    migrations.py      # The versioned migrations, per backend (see database.md for the count)
    operations.py      # Pure-function CRUD (all SQL lives here)
  fetchers/
    __init__.py        # Registers bmnews sources into bmlib's registry
    europepmc.py       # Europe PMC REST API client
  scoring/
    relevance_agent.py # LLM-based relevance scoring (BaseAgent subclass)
    scorer.py          # Orchestrates relevance + quality scoring
  transparency/
    service.py         # run_transparency(): select, analyse, store — informs only
  notify/
    watches.py         # Watch/Channel parsing and validation
    matcher.py         # Pure (paper, watch) -> bool
    service.py         # run_notify(): select, page, dispatch, record
    channels/          # Delivery adapters (email, Matrix)
  digest/
    renderer.py        # Jinja2 rendering (HTML + text)
    sender.py          # SMTP email delivery
  gui/                 # Flask + HTMX + pywebview desktop app
templates/             # Built-in Jinja2 templates (digest, notify, prompts)
tests/                 # pytest test suite
docs/plans/            # Design documents and implementation plans
```

Papers themselves live in **bmlib's** `publications` table, not in bmnews. See [Database](database.md).

## Getting started

```bash
git clone https://github.com/hherb/BioMedicalNews.git
cd BioMedicalNews
uv pip install -e ".[all]"
uv run pytest
```

Always use `uv` for package operations in this project — never `pip` directly.
