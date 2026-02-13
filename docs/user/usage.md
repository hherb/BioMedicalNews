# CLI Commands & Workflows

## Global options

These options apply to all commands:

```
bmnews [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
|--------|-------------|
| `-c, --config PATH` | Path to config file. Default: `~/.bmnews/config.toml` |
| `-v, --verbose` | Enable debug logging (shows API calls, SQL queries, LLM prompts) |
| `--version` | Print version and exit |

## Commands

### `bmnews run`

Run the full pipeline: fetch, store, score, and deliver a digest.

```bash
bmnews run [--days N] [--show_cached]
```

| Flag | Description |
|------|-------------|
| `--days N` | Override the `lookback_days` setting. Fetch papers from the last N days. |
| `--show_cached` | Skip the pipeline. Instead, re-display papers from previous digests. |

**Examples:**

```bash
# Standard run — fetch last 7 days (default), score, digest
bmnews run

# Fetch the last 14 days
bmnews run --days 14

# Re-read previous digest results
bmnews run --show_cached

# Show cached papers from the last 3 days only
bmnews run --show_cached --days 3
```

When run without `--show_cached`, the pipeline executes these stages in order:

1. **Fetch** — call enabled source APIs for papers in the lookback window
2. **Store** — upsert papers into the database (duplicates are updated, not duplicated)
3. **Score** — send unscored papers to the LLM for relevance scoring and quality assessment
4. **Digest** — render and deliver (email, file, or stdout) the top papers

### `bmnews fetch`

Fetch papers from configured sources and store them, without scoring or digesting.

```bash
bmnews fetch [--days N]
```

| Flag | Description |
|------|-------------|
| `--days N` | Override `lookback_days`. |

Useful when you want to accumulate papers over time and score them later in a batch.

```bash
bmnews fetch --days 30
# Fetched and stored 247 papers.
```

### `bmnews score`

Score all unscored papers in the database. Does not fetch new papers or generate a digest.

```bash
bmnews score
```

```bash
bmnews score
# Scored 42 papers.
```

### `bmnews digest`

Generate and deliver a digest from already-scored papers. Does not fetch or score.

```bash
bmnews digest [-o OUTPUT]
```

| Flag | Description |
|------|-------------|
| `-o, --output PATH` | Write the HTML digest to a file instead of emailing/printing. |

```bash
# Print to terminal
bmnews digest

# Save to file
bmnews digest -o ~/Desktop/digest.html
```

Delivery order:
1. If `-o` is given, write HTML to that file
2. If `[email]` is enabled and configured, send an email
3. Otherwise, print plain-text to stdout

### `bmnews init`

Initialize the database and create a default config file.

```bash
bmnews init [--config-path PATH]
```

| Flag | Description |
|------|-------------|
| `--config-path PATH` | Where to create the config file. Default: `~/.bmnews/config.toml` |

If a config file already exists at the target path, it is not overwritten.

### `bmnews search`

Search your stored papers by keyword. Searches titles and abstracts.

```bash
bmnews search QUERY
```

```bash
bmnews search "immunotherapy"
#   10.1101/2026.02.10.12345 [0.87] — Checkpoint Inhibitor Efficacy in TNBC: A Phase I
#   10.1101/2026.02.09.67890 [0.72] — Tumor Microenvironment Remodeling After Anti-PD-
```

Results are sorted by combined score (highest first). Shows up to 20 results.

## Common workflows

### Daily digest via cron

Add to your crontab (`crontab -e`):

```
# Run at 7:00 AM every day
0 7 * * * /path/to/bmnews run 2>> ~/.bmnews/bmnews.log
```

Make sure:
- The cron environment has access to your Python installation
- Your LLM provider is accessible (Ollama must be running, or Anthropic API key must be set)
- Email settings are configured if you want email delivery

### Catch up after being away

If you haven't run bmnews for a while:

```bash
# Fetch the last 30 days
bmnews run --days 30
```

### Batch workflow (fetch now, score later)

Useful if you run Ollama on a different machine or want to fetch during off-hours:

```bash
# Step 1: Fetch papers (fast, no LLM needed)
bmnews fetch --days 7

# Step 2: Score when LLM is available
bmnews score

# Step 3: Generate digest
bmnews digest
```

### Searching your archive

Over time, your database accumulates a useful archive of papers:

```bash
bmnews search "CRISPR"
bmnews search "meta-analysis"
bmnews search "randomized controlled"
```

### Reviewing past digests

```bash
# See all previously digested papers
bmnews run --show_cached

# Only from the last week
bmnews run --show_cached --days 7
```

### Using multiple configs

You can maintain separate configurations for different research topics:

```bash
bmnews -c ~/.bmnews/oncology.toml run
bmnews -c ~/.bmnews/neuroscience.toml run
```

Each config can have different research interests, sources, and scoring thresholds. They share the same database by default (or use separate databases if you configure different `sqlite_path` values).
