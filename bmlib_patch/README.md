# bmlib patch / source archive

This directory contains the complete bmlib source for the `hherb/bmlib` repository.

## Files

- **`bmlib_source.tar.gz`** — Complete bmlib source tree (extract to get `/bmlib/` directory)
- **`bmlib_initial.patch`** — Git patch of all staged files (for `git apply`)

## How to use (from the bmlib repo)

### Option 1: Extract the tarball

```bash
cd /home/user
tar xzf /path/to/bmlib_source.tar.gz
cd bmlib
git add -A
git commit -m "Initial bmlib: shared library for biomedical literature tools"
git push -u origin main
```

### Option 2: Apply the patch

```bash
cd /home/user/bmlib
git apply /path/to/bmlib_initial.patch
git commit -m "Initial bmlib: shared library for biomedical literature tools"
git push -u origin main
```

## What's in bmlib

- **bmlib.llm** — LLM provider abstraction (Ollama native API, Anthropic) with token tracking
- **bmlib.db** — Pure-function database abstraction (SQLite, PostgreSQL)
- **bmlib.templates** — Jinja2 template engine with user/default fallback directories
- **bmlib.agents** — Base agent class accepting external LLM config
- **bmlib.quality** — 3-tier quality assessment pipeline (metadata, classifier, deep)
- **bmlib.transparency** — Multi-API transparency and bias analysis

All 52 tests passing.
