# Configuration Reference

All configuration lives in a single TOML file at `~/.bmnews/config.toml`. Run `bmnews init` to generate a default config with sensible starting values.

You can override the config path per invocation with `bmnews -c /path/to/config.toml`.

## `[general]`

Top-level application settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `log_level` | string | `"INFO"` | Logging verbosity. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `template_dir` | string | `""` | Path to a directory with custom Jinja2 templates. Templates here override the built-in defaults. See [Templates](templates.md). |

```toml
[general]
log_level = "INFO"
template_dir = "~/.bmnews/templates"
```

## `[database]`

Database backend and connection settings. bmnews supports SQLite (zero-config, file-based) and PostgreSQL (for multi-user or production setups).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"sqlite"` | Database backend: `"sqlite"` or `"postgresql"`. |
| `sqlite_path` | string | `"~/.bmnews/bmnews.db"` | Path to the SQLite database file. Tilde is expanded. |
| `pg_dsn` | string | `""` | PostgreSQL DSN connection string. If set, other `pg_*` keys are ignored. |
| `pg_host` | string | `"localhost"` | PostgreSQL host. |
| `pg_port` | integer | `5432` | PostgreSQL port. |
| `pg_database` | string | `"bmnews"` | PostgreSQL database name. |
| `pg_user` | string | `"bmnews"` | PostgreSQL user. |
| `pg_password` | string | `""` | PostgreSQL password. |

### SQLite (default)

```toml
[database]
backend = "sqlite"
sqlite_path = "~/.bmnews/bmnews.db"
```

No setup needed. The database file is created automatically by `bmnews init`.

### PostgreSQL

```toml
[database]
backend = "postgresql"
pg_host = "localhost"
pg_port = 5432
pg_database = "bmnews"
pg_user = "bmnews"
pg_password = "secret"
```

Or with a DSN:

```toml
[database]
backend = "postgresql"
pg_dsn = "postgresql://bmnews:secret@localhost:5432/bmnews"
```

Requires the `postgresql` optional dependency: `pip install -e ".[postgresql]"`.

## `[sources]`

Control which preprint servers to fetch from and how far back to look.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `medrxiv` | boolean | `true` | Fetch from medRxiv (medical preprints). |
| `biorxiv` | boolean | `false` | Fetch from bioRxiv (biology preprints). |
| `europepmc` | boolean | `true` | Fetch from Europe PMC (broad biomedical literature). |
| `lookback_days` | integer | `7` | How many days back to fetch. Can be overridden with `--days` on the CLI. |
| `europepmc_query` | string | `""` | Optional search query for Europe PMC. If empty, fetches recent preprints (PPR source). If set, searches for that query within the lookback window. |

```toml
[sources]
medrxiv = true
biorxiv = true
europepmc = true
lookback_days = 7
europepmc_query = "cancer immunotherapy"
```

### About the sources

- **medRxiv** — medical sciences preprints. Tends to have clinical trials, epidemiology, public health.
- **bioRxiv** — biology preprints. Broader — molecular biology, neuroscience, genomics, etc.
- **Europe PMC** — aggregates preprints and published articles from PubMed, PMC, and preprint servers. With a custom `europepmc_query`, you can target specific topics across all indexed literature.

## `[llm]`

LLM provider settings for relevance scoring and summary generation.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `provider` | string | `"ollama"` | LLM provider: `"ollama"` (local) or `"anthropic"` (Claude API). |
| `model` | string | `""` | Model identifier. Format: `"provider:model_name"` (e.g., `"ollama:llama3.1"`, `"anthropic:claude-sonnet-4-5-20250929"`). |
| `temperature` | float | `0.3` | Sampling temperature. Lower = more deterministic scoring. `0.3` is a good default for structured tasks. |
| `max_tokens` | integer | `4096` | Maximum tokens in the LLM response. |
| `ollama_host` | string | `""` | Ollama API endpoint. Leave empty for the default (`http://localhost:11434`). |
| `anthropic_api_key` | string | `""` | Anthropic API key. Can also be set via `ANTHROPIC_API_KEY` environment variable. |
| `concurrency` | integer | `1` | Number of papers to score in parallel. Use `1` for Ollama (local). Higher values (e.g., `5`) for Anthropic API to speed up scoring. |

### Ollama (local)

```toml
[llm]
provider = "ollama"
model = "ollama:llama3.1"
temperature = 0.3
concurrency = 1
```

### Anthropic Claude

```toml
[llm]
provider = "anthropic"
model = "anthropic:claude-sonnet-4-5-20250929"
anthropic_api_key = "sk-ant-..."
temperature = 0.3
concurrency = 5
```

## `[scoring]`

Thresholds that control which papers make it into the digest.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `min_relevance` | float | `0.5` | Minimum relevance score (0.0–1.0) for a paper to be considered. Papers below this are still stored but won't appear in digests. |
| `min_combined` | float | `0.4` | Minimum combined score (0.0–1.0) for digest inclusion. Combined = 60% relevance + 40% quality. |

```toml
[scoring]
min_relevance = 0.5
min_combined = 0.4
```

**Tuning tips:**
- If you get too many papers, raise `min_combined` to `0.5` or `0.6`.
- If you're missing relevant papers, lower `min_relevance` to `0.3`.
- The combined score weights relevance (60%) more than quality (40%), so a highly relevant case report can still make the cut.

## `[quality]`

Quality assessment settings. bmnews uses bmlib's 3-tier quality pipeline to evaluate study methodology.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` | Enable quality assessment. If disabled, quality_score defaults to 0.0. |
| `default_tier` | integer | `2` | Default assessment tier (1–3). See below. |
| `max_tier` | integer | `3` | Maximum tier to use. |
| `min_quality_tier` | string | `"TIER_1_ANECDOTAL"` | Minimum quality tier for digest inclusion. |

### Quality tiers

| Tier | Method | Cost | Description |
|------|--------|------|-------------|
| 1 | Metadata | Free | Classifies study design from publication type metadata. Fast and free. |
| 2 | LLM Classifier | Low | Uses a small LLM to classify study design from title/abstract. |
| 3 | Deep Assessment | Higher | Full LLM assessment of methodology, bias risk, sample size. |

### Evidence hierarchy

Papers are classified into one of five quality tiers:

| Quality Tier | Score | Study Designs |
|-------------|-------|---------------|
| `TIER_5_SYNTHESIS` | 0.95 | Systematic reviews, meta-analyses |
| `TIER_4_EXPERIMENTAL` | 0.85 | Randomized controlled trials |
| `TIER_3_CONTROLLED` | 0.70 | Cohort studies, case-control studies |
| `TIER_2_DESCRIPTIVE` | 0.50 | Cross-sectional studies, case series |
| `TIER_1_ANECDOTAL` | 0.30 | Case reports, editorials, letters |

```toml
[quality]
enabled = true
default_tier = 2
max_tier = 3
min_quality_tier = "TIER_1_ANECDOTAL"
```

## `[transparency]`

Optional transparency analysis using multiple external APIs to assess publication bias and integrity.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Enable transparency analysis. Requires the `transparency` optional dependency. |
| `min_score_threshold` | float | `0.6` | Only run transparency analysis on papers with a combined score above this threshold (saves API calls). |

```toml
[transparency]
enabled = false
min_score_threshold = 0.6
```

When enabled, queries CrossRef, EuropePMC, OpenAlex, and ClinicalTrials.gov for additional metadata about each paper's publication history, funding, and trial registration.

## `[user]`

Your identity and research profile.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | `"Your Name"` | Your name (used in email headers). |
| `email` | string | `"your@email.com"` | Your email address. Also used as the default `to_address` for digests. |
| `research_interests` | list of strings | `["clinical trials", "oncology"]` | Your research interests. The LLM uses these to judge paper relevance. Be specific. |

```toml
[user]
name = "Dr. Jane Doe"
email = "jane.doe@university.edu"
research_interests = [
    "triple-negative breast cancer",
    "checkpoint inhibitor combination therapy",
    "tumor microenvironment and immune evasion",
    "biomarkers for immunotherapy response prediction",
]
```

**Tips for good research interests:**
- Be specific: "PD-L1 expression in non-small cell lung cancer" beats "cancer"
- Include methodological interests: "randomized controlled trials" or "single-cell RNA sequencing"
- List 3–8 interests for best results
- Use the terminology you'd search PubMed with

## `[email]`

SMTP settings for email digest delivery. When disabled, digests print to stdout instead.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Enable email delivery. |
| `smtp_host` | string | `""` | SMTP server hostname. |
| `smtp_port` | integer | `587` | SMTP port. 587 for TLS, 465 for SSL, 25 for unencrypted. |
| `smtp_user` | string | `""` | SMTP login username. |
| `smtp_password` | string | `""` | SMTP login password. |
| `use_tls` | boolean | `true` | Use STARTTLS encryption. |
| `from_address` | string | `""` | Sender email address. |
| `to_address` | string | `""` | Recipient email address. Defaults to `user.email` if empty. |
| `subject_prefix` | string | `"[BioMedNews]"` | Subject line prefix for digest emails. |
| `max_papers` | integer | `20` | Maximum number of papers to include in a single digest. |

### Gmail example

```toml
[email]
enabled = true
smtp_host = "smtp.gmail.com"
smtp_port = 587
smtp_user = "your.email@gmail.com"
smtp_password = "your-app-password"
use_tls = true
from_address = "your.email@gmail.com"
to_address = "your.email@gmail.com"
subject_prefix = "[BioMedNews]"
max_papers = 20
```

For Gmail, you need an [App Password](https://support.google.com/accounts/answer/185833) (not your regular password).

### Institutional SMTP

```toml
[email]
enabled = true
smtp_host = "mail.university.edu"
smtp_port = 587
smtp_user = "jane.doe"
smtp_password = "..."
use_tls = true
from_address = "jane.doe@university.edu"
to_address = "jane.doe@university.edu"
```

## `[notifications]`

Watches alert you about a matching paper as it is scored, separately from the periodic digest. A notified paper **still appears** in the next digest — a notification is "now", the digest is the record.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Master switch. When false, no watch is evaluated. |

Channels and watches are named sub-tables. Both are keyed by name (`[notifications.watches.melanoma-trials]`) rather than being arrays of tables — that is what survives a round trip through the settings UI.

### Channels — where a notification goes

`[notifications.channels.<name>]` with a `kind` selecting the adapter:

| Key | Kind | Description |
|-----|------|-------------|
| `kind` | — | `"email"` or `"matrix"`. Required. |
| `to_address` | email | Recipient. Falls back to `email.to_address`, then `user.email`. |
| `subject_prefix` | email | Overrides `email.subject_prefix` for this channel. |
| `homeserver` | matrix | Base URL, e.g. `https://matrix.example.org`. Required, and must be `https` — see below. |
| `access_token` | matrix | Bearer token for the sending account. Required. |
| `room` | matrix | Room id (`!abc:server`) or alias (`#alerts:server`). Required. |

An `email` channel reuses the SMTP settings from `[email]`, so a working digest already has everything it needs. It does **not** consult `email.enabled` — that switches the digest email off, and a watch is a separate subscription. Turn watches off with `[notifications] enabled` or the watch's own `enabled`.

**Matrix rooms must not be encrypted.** A bot posting over the plain HTTP API cannot produce a readable message in an end-to-end encrypted room, so bmnews refuses to post there rather than filling it with ciphertext nobody can read and reporting success. This is a deliberate limitation, not a temporary one: the content is alerts about public preprints, so there is nothing confidential to protect.

The `access_token` sits in `config.toml` next to `smtp_password` — same exposure, same precedent. It goes to the homeserver as a bearer token on every request and does not expire on its own, so **the `homeserver` URL must be `https`**. A plain `http://` one is refused, except to `localhost` or `127.0.0.1`, where nothing leaves the machine.

### Watches — what is worth alerting about

`[notifications.watches.<name>]`. Every criterion is AND-combined, and an **empty list means "no constraint"**. Within one list criterion the test is "any": two tags mean *either* tag, not both.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` | Whether the watch is evaluated. |
| `min_relevance` | float | `0.0` | Floor on the LLM relevance score, 0.0–1.0. |
| `min_combined` | float | `0.0` | Floor on the combined score, 0.0–1.0. |
| `min_quality_tier` | string | `""` | Tier floor, e.g. `"TIER_4_EXPERIMENTAL"`. `UNCLASSIFIED` papers are never excluded by it. |
| `tags` | list | `[]` | Interest tags the scorer matched. |
| `keywords` | list | `[]` | Case-insensitive substrings of title or abstract. "melanoma" finds "melanomas". |
| `sources` | list | `[]` | Source names, e.g. `["medrxiv", "pubmed"]`. |
| `journals` | list | `[]` | Journal names, compared case-insensitively. |
| `study_designs` | list | `[]` | Study designs, e.g. `["rct", "meta_analysis"]`. |
| `channels` | list | `[]` | Names of channels above. A watch with none delivers nowhere. |
| `max_per_run` | integer | `5` | How many papers one run delivers. The rest stay queued. |

A mistyped criterion key is reported in the log rather than silently ignored, and a value that cannot mean anything (an unknown tier, a score above 1.0) makes bmnews skip that watch with an error instead of matching on the rest of it.

### Example

```toml
[notifications]
enabled = true

[notifications.channels.mail]
kind = "email"                       # reuses the [email] SMTP settings
to_address = "alerts@example.org"

[notifications.channels.chat]
kind = "matrix"
homeserver = "https://matrix.example.org"
access_token = "syt_..."
room = "#bmnews-alerts:example.org"

[notifications.watches.melanoma-trials]
enabled = true
min_relevance = 0.8
min_quality_tier = "TIER_4_EXPERIMENTAL"
tags = ["melanoma", "immunotherapy"]
study_designs = ["rct"]
channels = ["mail", "chat"]
max_per_run = 5
```

Check it against papers you already have before turning it loose:

```bash
bmnews notify --watch melanoma-trials --dry-run
```

Loosening a watch's threshold makes it match papers already in the database, so history resurfaces. That is intended — and it is exactly why `max_per_run` pages rather than truncates.

## Complete example config

```toml
[general]
log_level = "INFO"

[database]
backend = "sqlite"
sqlite_path = "~/.bmnews/bmnews.db"

[sources]
medrxiv = true
biorxiv = true
europepmc = true
lookback_days = 7

[llm]
provider = "ollama"
model = "ollama:llama3.1"
temperature = 0.3
max_tokens = 4096
concurrency = 1

[scoring]
min_relevance = 0.5
min_combined = 0.4

[quality]
enabled = true
default_tier = 2
max_tier = 3
min_quality_tier = "TIER_1_ANECDOTAL"

[transparency]
enabled = false
min_score_threshold = 0.6

[user]
name = "Dr. Jane Doe"
email = "jane.doe@university.edu"
research_interests = [
    "triple-negative breast cancer",
    "checkpoint inhibitor combination therapy",
    "tumor microenvironment",
]

[email]
enabled = false
smtp_host = "smtp.gmail.com"
smtp_port = 587
smtp_user = ""
smtp_password = ""
use_tls = true
from_address = ""
to_address = ""
subject_prefix = "[BioMedNews]"
max_papers = 20

[notifications]
enabled = false

[notifications.channels.mail]
kind = "email"
to_address = "jane.doe@university.edu"

[notifications.watches.tnbc-trials]
enabled = true
min_relevance = 0.8
tags = ["triple-negative breast cancer"]
study_designs = ["rct"]
channels = ["mail"]
max_per_run = 5
```
