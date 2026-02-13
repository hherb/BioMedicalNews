# BioMedical News — User Manual

BioMedical News (`bmnews`) is a command-line tool that fetches biomedical preprints from multiple sources, scores them for relevance to your research interests using LLM-based assessment, evaluates their methodological quality, and delivers curated digests via email or terminal.

## What it does

1. **Fetches** recent preprints from medRxiv, bioRxiv, and Europe PMC
2. **Scores** each paper for relevance to your research interests using an LLM
3. **Assesses** methodological quality using a 3-tier evidence hierarchy
4. **Delivers** a ranked digest of the most relevant, highest-quality papers

## Key features

- **Multi-source fetching** — medRxiv, bioRxiv, and Europe PMC
- **LLM-based relevance scoring** — works with Ollama (local/free) or Anthropic Claude (API)
- **Quality assessment** — metadata-based classification into evidence tiers (anecdotal through synthesis)
- **Configurable interests** — define your research topics, get papers that matter to you
- **Email digests** — HTML and plain-text email delivery via SMTP
- **Template customization** — override LLM prompts and digest layout with Jinja2 templates
- **Database storage** — SQLite (default) or PostgreSQL for paper history and search
- **Keyword search** — search your accumulated paper archive

## Documentation

| Guide | Description |
|-------|-------------|
| [Installation](installation.md) | Prerequisites, install, LLM setup |
| [Quick Start](quickstart.md) | Get your first digest in 5 minutes |
| [Configuration](configuration.md) | Full config reference — every setting documented |
| [Usage](usage.md) | CLI commands, flags, and common workflows |
| [Templates](templates.md) | Customizing prompts and digest appearance |
| [Troubleshooting](troubleshooting.md) | Common problems and solutions |

## Quick taste

```bash
pip install -e ".[ollama]"
bmnews init
# Edit ~/.bmnews/config.toml — set your research interests
bmnews run
```

That's it. You'll see a digest of recent preprints ranked by relevance to your interests, printed to the terminal.
