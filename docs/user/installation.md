# Installation

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the package installer this project uses:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Python 3.11 or later** — check with `python3 --version`. You do not have to
  install one yourself: `uv venv --python 3.11` fetches a suitable Python if
  your system has none.
- **An LLM provider** — either Ollama (local, free) or an Anthropic API key

## Install bmnews

Clone the repository, create a virtual environment, and install in editable mode:

```bash
git clone https://github.com/hherb/BioMedicalNews.git
cd BioMedicalNews
uv venv                      # creates .venv/
source .venv/bin/activate    # Windows: .venv\Scripts\activate
uv pip install -e .
```

The virtual environment is not optional: unlike `pip`, `uv pip install` refuses
to install without one rather than writing into your system Python. If you would
rather not activate it, prefix commands with `uv run` instead — `uv run bmnews
init` works from the project directory with no activation at all.

### Optional dependency groups

Install only what you need:

```bash
# Ollama support (local LLM)
uv pip install -e ".[ollama]"

# Anthropic Claude support (API)
uv pip install -e ".[anthropic]"

# PostgreSQL backend (instead of SQLite)
uv pip install -e ".[postgresql]"

# Desktop GUI (pywebview + Flask) — needed for `bmnews gui`
uv pip install -e ".[gui]"

# Transparency analysis (research-integrity checks via bmlib). This extra
# resolves to httpx>=0.25, which bmnews already requires as a core
# dependency — it installs nothing new. It exists as a marker for the
# feature rather than a real dependency boundary; enable it in config with
# [transparency] enabled = true, no extra install needed.
uv pip install -e ".[transparency]"

# Development tools (pytest, ruff)
uv pip install -e ".[dev]"

# Everything
uv pip install -e ".[all]"
```

### Verify installation

```bash
bmnews --version
```

You should see `bmnews, version 0.3.0` (or the current version).

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
