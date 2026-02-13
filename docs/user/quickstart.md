# Quick Start

Get your first digest in 5 minutes.

## 1. Install and initialize

```bash
cd BioMedicalNews
pip install -e ".[ollama]"
bmnews init
```

## 2. Edit your config

Open `~/.bmnews/config.toml` in your editor and change these sections:

```toml
[llm]
provider = "ollama"
model = "ollama:llama3.1"

[user]
name = "Jane Doe"
email = "jane@example.com"
research_interests = [
    "breast cancer immunotherapy",
    "checkpoint inhibitors",
    "tumor microenvironment",
]

[sources]
medrxiv = true
biorxiv = false
europepmc = true
lookback_days = 7
```

The `research_interests` list is the most important setting. The LLM uses these to judge how relevant each paper is to your work. Be specific — "checkpoint inhibitors in triple-negative breast cancer" works better than "cancer".

## 3. Run the pipeline

```bash
bmnews run
```

This executes the full cycle:

1. **Fetch** — downloads recent preprints from your enabled sources
2. **Store** — saves them in the local database
3. **Score** — asks the LLM to rate each paper's relevance and generate a summary
4. **Digest** — prints a ranked list of the best matches

Example output:

```
[BioMedNews] Biomedical News Digest
====================================

5 new publications matching your research interests.

1. Checkpoint Inhibitor Efficacy in TNBC: A Phase II Trial
   https://doi.org/10.1101/2026.02.10.12345
   Authors: Smith J; Wang L; Patel R
   Date: 2026-02-10
   Source: medrxiv
   Relevance: 92% | Quality: TIER_4_EXPERIMENTAL | Design: rct

   This phase II trial demonstrates significant improvement in progression-free
   survival with combination checkpoint inhibitor therapy in triple-negative
   breast cancer patients...

2. Tumor Microenvironment Remodeling After Anti-PD-1 Therapy
   ...
```

## 4. What just happened?

Behind the scenes, bmnews:

- Called the medRxiv and Europe PMC APIs to fetch papers from the last 7 days
- Stored each paper's metadata and abstract in `~/.bmnews/bmnews.db`
- Sent each unscored paper's title and abstract to your LLM with your research interests as context
- The LLM returned a relevance score (0–100%), a summary, and key findings for each paper
- Assessed each paper's methodological quality from its metadata (study design, publication type)
- Computed a combined score: 60% relevance + 40% quality
- Filtered papers above the minimum threshold and printed the top results

## 5. Next steps

- **Set up email delivery** — add your SMTP settings in `[email]` to receive digests by email. See [Configuration](configuration.md).
- **Schedule it** — add `bmnews run` to a cron job for daily digests:
  ```
  0 7 * * * /path/to/bmnews run
  ```
- **Search your archive** — find papers you've already fetched:
  ```bash
  bmnews search "immunotherapy"
  ```
- **Review cached digests** — re-read previous digests:
  ```bash
  bmnews run --show_cached
  bmnews run --show_cached --days 7
  ```
- **Customize templates** — change how the LLM scores papers or how digests look. See [Templates](templates.md).
