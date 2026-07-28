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

Run the full pipeline: fetch, store, score, deliver any watch notifications, and deliver a digest.

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

### `bmnews notify`

Deliver **watch** notifications — alerts about individual papers matching criteria you named, separate from the periodic digest. A notified paper still appears in the next digest.

Watches are configured under `[notifications]`; see the [configuration reference](configuration.md#notifications).

```bash
bmnews notify [--watch NAME] [--count N] [--all] [--dry-run] [--list]
```

| Flag | Description |
|------|-------------|
| `--watch NAME` | Only act on this watch. Default: every enabled watch. |
| `--count N` | Deliver this many papers per watch, overriding its `max_per_run`. Must be at least 1. |
| `--all` | Deliver every pending match rather than one batch. Cannot be combined with `--count` — both set the batch size. |
| `--dry-run` | Report what would be sent. Delivers nothing and records nothing. |
| `--list` | Report each watch's counts. Delivers nothing. |

**Nothing is ever silently dropped.** A watch's `max_per_run` bounds one batch; the rest stay queued and the command tells you how many are left. Run it again to pull the next batch:

```bash
# What is waiting, per watch and channel — including disabled watches
bmnews notify --list
# melanoma-trials → matrix: 12 delivered, 20 matching, 8 remaining

# Send the next batch (max_per_run each)
bmnews notify

# Send the next 3 only
bmnews notify --watch melanoma-trials --count 3

# Drain everything still queued
bmnews notify --all
```

**Tuning a watch** is what `--dry-run` is for: it replays the criteria against the papers you have already stored, so you can see what a watch would fire on without waiting for a fetch and without sending anything.

```bash
bmnews notify --watch melanoma-trials --dry-run
```

A delivery that fails is recorded as failed and stays queued, so the next run retries it — per channel, so a watch that alerts both email and Matrix retries only the one that broke. If every delivery in a run fails, the command exits non-zero, which is what makes a failure visible from cron.

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

### Being alerted about specific papers

The digest is a periodic roundup. A watch is "if a paper like *this* turns up, tell me now":

```bash
# Configure a watch under [notifications.watches.<name>], then check it
# against papers you already have before turning it loose:
bmnews notify --watch melanoma-trials --dry-run

# `bmnews run` delivers watch notifications as part of the pipeline, so
# a cron entry needs nothing extra. To alert more often than you fetch:
0 * * * * /usr/local/bin/bmnews notify
```

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
