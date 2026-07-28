# Multi-Provider LLM Support + Auto-Populated Model Selector

**Date**: 2026-02-15
**Status**: Approved

## Problem

The settings UI has a hardcoded provider dropdown (Ollama, Anthropic) and a plain text input for model names. Users must know and type exact model identifiers. We need to:

1. Add DeepSeek, Mistral, OpenAI, and Gemini providers
2. Auto-populate the model selector from each provider's API
3. Cache model lists persistently until manually refreshed

## Design

### Part A: OpenAI-Compatible Provider Base (bmlib)

All four new providers (OpenAI, DeepSeek, Mistral, Gemini) support the OpenAI chat completions API format. A shared base class minimizes code duplication.

**New file: `bmlib/llm/providers/openai_compat.py`**

`OpenAICompatibleProvider(BaseProvider)` uses the `openai` Python SDK with configurable `base_url`.

- `chat()`: Separates system messages, handles JSON mode, maps temperature/max_tokens/top_p
- `list_models()`: Calls `/v1/models`, caches with TTL, falls back to hardcoded `FALLBACK_MODELS`
- `test_connection()`: Lightweight models list call
- `count_tokens()`: Character-based estimation (~4 chars per token)

Subclass protocol: override class attrs + `FALLBACK_MODELS` + `MODEL_PRICING`.

**New files (~30 lines each):**

| File | Provider | Base URL | API Key Env | Default Model |
|------|----------|----------|-------------|---------------|
| `openai_provider.py` | OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-4o` |
| `deepseek.py` | DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `mistral.py` | Mistral | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | `mistral-large-latest` |
| `gemini.py` | Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` | `gemini-2.0-flash` |

**Updated: `providers/__init__.py`** — Register 4 new providers in `_ensure_builtins()`.

### Part B: Settings UI (BioMedicalNews)

**New route: `GET /settings/models?provider=<name>`**

Returns JSON list of `[{id, display_name}, ...]` for the model datalist.

- Tries provider's `list_models()` API first
- Falls back to hardcoded `FALLBACK_MODELS` on failure
- Caches to `~/.bmnews/model_cache.json` indefinitely per provider
- `?refresh=1` param busts cache and re-fetches from API

**Updated: `settings.html` LLM section**

- Provider `<select>` with 6 options, HTMX triggers model fetch on change
- Model `<input type="text">` with `<datalist>` populated by HTMX response
- API key `<input type="password">` field
- Refresh button (icon) next to model selector

**Updated: `config.py`**

`LLMConfig` gets `api_key` and `base_url` fields for generic provider support.

**Updated: `client.py`**

`LLMClient.__init__` accepts generic provider config dict, passes appropriate kwargs to each provider.

### Part C: LLMClient Generalization

Current `LLMClient.__init__` hardcodes Ollama/Anthropic-specific kwargs. Updated to accept a generic `provider_config` dict keyed by provider name, so new providers get their API keys and base URLs passed through automatically.

## File Changes Summary

### New files (bmlib)
- `bmlib/llm/providers/openai_compat.py` — Base class (~150 lines)
- `bmlib/llm/providers/openai_provider.py` — OpenAI (~40 lines)
- `bmlib/llm/providers/deepseek.py` — DeepSeek (~40 lines)
- `bmlib/llm/providers/mistral.py` — Mistral (~40 lines)
- `bmlib/llm/providers/gemini.py` — Gemini (~40 lines)

### Modified files (bmlib)
- `bmlib/llm/providers/__init__.py` — Register new providers
- `bmlib/llm/client.py` — Generalize provider config passing

### Modified files (BioMedicalNews)
- `bmnews/config.py` — Add `api_key`, `base_url` to LLMConfig
- `bmnews/gui/routes/settings.py` — Add `/settings/models` endpoint
- `bmnews/gui/templates/fragments/settings.html` — Redesigned LLM section
- `bmnews/gui/static/js/app.js` — Model datalist population on provider change
