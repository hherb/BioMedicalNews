# Troubleshooting

## No papers fetched

**Symptom:** `bmnews fetch` or `bmnews run` reports "No papers fetched" or 0 papers.

**Causes:**
- **Sources disabled** — check `[sources]` in config. At least one of `medrxiv`, `biorxiv`, or `europepmc` must be `true`.
- **Lookback too short** — some servers have sparse days. Try `bmnews fetch --days 14`.
- **Network issues** — the medRxiv/bioRxiv API or Europe PMC may be down. Run with `-v` to see HTTP errors.
- **Europe PMC query too narrow** — if you set `europepmc_query`, it may be too restrictive. Try removing it to fetch all recent preprints.

**Debug:**
```bash
bmnews -v fetch --days 14
```

This shows the exact API URLs being called and any HTTP errors.

## Low relevance scores / irrelevant results

**Symptom:** The digest contains papers that don't match your interests, or everything scores low.

**Causes:**
- **Vague research interests** — "biology" or "medicine" matches almost everything poorly. Be specific: "CAR-T cell therapy in B-cell lymphoma" is much better.
- **Wrong LLM model** — very small models (< 3B parameters) may not understand biomedical terminology well. Try a larger model.
- **Temperature too high** — the default `0.3` is good. Higher values add randomness.

**Fix:**
1. Edit `[user].research_interests` to be more specific
2. Try a larger/better model
3. Run `bmnews score` again to re-score (only unscored papers are processed, so you may need to delete scores first)

## Scoring errors or timeouts

**Symptom:** Scoring fails with errors, or hangs indefinitely.

**Causes:**
- **Ollama not running** — start Ollama: `ollama serve`
- **Model not pulled** — `ollama pull llama3.1`
- **Wrong model name** — model must exist in your Ollama installation or be a valid Anthropic model
- **API key invalid** — for Anthropic, check your `anthropic_api_key`

**Debug:**
```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# Check model exists
ollama list

# Run with verbose logging
bmnews -v score
```

## Email not sending

**Symptom:** `bmnews run` completes but no email arrives.

**Causes:**
- **Email disabled** — set `[email].enabled = true`
- **Missing SMTP settings** — `smtp_host`, `from_address`, and either `to_address` or `[user].email` must be set
- **Wrong credentials** — check `smtp_user` and `smtp_password`
- **Gmail requires App Password** — regular passwords don't work. Create an [App Password](https://support.google.com/accounts/answer/185833).
- **Firewall blocking port 587** — try port 465 with `use_tls = true`, or check your network/firewall settings
- **TLS mismatch** — some servers use implicit TLS on port 465, not STARTTLS on 587

**Debug:**
```bash
bmnews -v digest
```

Look for "Failed to send digest email" in the output. The verbose log will show the SMTP error.

**Test with file output first:**
```bash
bmnews digest -o /tmp/test_digest.html
open /tmp/test_digest.html
```

If the HTML file looks correct, the problem is with SMTP settings, not with digest generation.

## Database errors

**Symptom:** Errors mentioning SQLite, tables, or "no such table".

**Causes:**
- **Database not initialized** — run `bmnews init`
- **Corrupt database** — rare, but possible after a crash. Back up and reinitialize:
  ```bash
  cp ~/.bmnews/bmnews.db ~/.bmnews/bmnews.db.bak
  rm ~/.bmnews/bmnews.db
  bmnews init
  ```
- **Wrong sqlite_path** — check `[database].sqlite_path` in your config

For PostgreSQL:
- Check that the database exists and the user has permissions
- Verify connection settings (`pg_host`, `pg_port`, `pg_database`, `pg_user`, `pg_password`)

## Config file not found

**Symptom:** bmnews uses defaults instead of your settings.

**Causes:**
- Config file doesn't exist at `~/.bmnews/config.toml`
- You're using a custom path but forgot `-c`: `bmnews -c /path/to/config.toml run`

bmnews logs "Config file not found ... using defaults" at INFO level. Run with `-v` to see this message.

## Papers scored but no digest generated

**Symptom:** `bmnews score` works, but `bmnews digest` says "No papers above threshold for digest."

**Causes:**
- **Thresholds too high** — lower `[scoring].min_combined` (e.g., from `0.4` to `0.3`)
- **Papers already digested** — each paper only appears in one digest. If you've already run `bmnews digest`, those papers won't appear again. Use `bmnews run --show_cached` to review them.
- **No papers scored above threshold** — the LLM may have scored everything low. Check your research interests.

**Check what's in the database:**
```bash
bmnews search ""
```

This shows all stored papers with their scores.

## Getting more help

- Run any command with `-v` for verbose debug output
- Check the log messages for specific error details
- File issues at https://github.com/hherb/BioMedicalNews/issues
