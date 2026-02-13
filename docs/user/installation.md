# Installation

## Prerequisites

- **Python 3.11 or later** — check with `python3 --version`
- **pip** or **uv** — Python package installer
- **An LLM provider** — either Ollama (local, free) or an Anthropic API key

## Install bmnews

Clone the repository and install in editable mode:

```bash
git clone https://github.com/hherb/BioMedicalNews.git
cd BioMedicalNews
pip install -e .
```

### Optional dependency groups

Install only what you need:

```bash
# Ollama support (local LLM)
pip install -e ".[ollama]"

# Anthropic Claude support (API)
pip install -e ".[anthropic]"

# PostgreSQL backend (instead of SQLite)
pip install -e ".[postgresql]"

# Transparency analysis (multi-API bias detection)
pip install -e ".[transparency]"

# Development tools (pytest, ruff)
pip install -e ".[dev]"

# Everything
pip install -e ".[all]"
```

### Verify installation

```bash
bmnews --version
```

You should see `bmnews, version 0.1.0` (or the current version).

## LLM setup

bmnews uses a large language model to score papers for relevance and generate summaries. You need at least one LLM provider.

### Option A: Ollama (local, free)

[Ollama](https://ollama.ai) runs models locally on your machine. No API keys, no costs, full privacy.

1. Install Ollama from https://ollama.ai
2. Pull a model:
   ```bash
   ollama pull llama3.1
   ```
3. In your config (`~/.bmnews/config.toml`):
   ```toml
   [llm]
   provider = "ollama"
   model = "ollama:llama3.1"
   ```

Any model that supports JSON output works. Smaller models (7B–8B parameters) are fastest; larger models (70B+) produce better scoring.

### Option B: Anthropic Claude (API)

[Anthropic](https://www.anthropic.com) provides Claude models via API. Higher quality, but costs money per token.

1. Get an API key from https://console.anthropic.com
2. In your config:
   ```toml
   [llm]
   provider = "anthropic"
   model = "anthropic:claude-sonnet-4-5-20250929"
   anthropic_api_key = "sk-ant-..."
   ```

You can also set the key via the `ANTHROPIC_API_KEY` environment variable instead of putting it in the config file.

## First-time initialization

After installing, run:

```bash
bmnews init
```

This creates:
- `~/.bmnews/config.toml` — configuration file with sensible defaults
- `~/.bmnews/bmnews.db` — SQLite database (empty, ready to go)

Now edit the config file to set your research interests and LLM provider. See [Configuration](configuration.md) for all available settings, or jump to [Quick Start](quickstart.md) to get going fast.
