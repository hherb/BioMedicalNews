# GUI

Desktop app: pywebview (native window) + Flask (HTTP backend) + HTMX (frontend interactivity).

**Layout:** Email-client style with tab bar, resizable split panes (Split.js), paper list with infinite scroll, reading pane with fulltext toggle, settings form, and pipeline status footer.

**Key features:**
- **HTMX fragment-based updates** — paper list pagination, paper detail, settings, and pipeline status polling (500ms interval) use partial HTML responses
- **Async pipeline execution** — runs in daemon thread with `on_progress` and `on_scored` callbacks; OOB (out-of-band) HTMX swaps update individual paper cards and refresh the list
- **Auto-resume** — on app startup, automatically scores any unscored papers
- **Fulltext retrieval** — on-demand via `bmlib.fulltext.FullTextService`, seeded with the URLs sync recorded in `fulltext_sources` and falling back to Europe PMC → Unpaywall → DOI; JATS XML parsed to HTML; cached in `paper_extras.fulltext_html`. When the text was extracted from a PDF, the reading pane offers **View PDF** alongside it. Every outbound URL passes `_safe_url()` first — these come from upstream services, and escaping stops attribute injection but not a `javascript:` payload
- **Dynamic model selector** — auto-populated from provider APIs with local caching
- **Window geometry persistence** — saves/restores position and size in `~/.bmnews/window_state.json`
- **Sorting/filtering** — by date, score, source, quality tier, study design

**Routes:**

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Main index (base.html) |
| GET | `/papers` | Paper list with pagination/filters |
| GET | `/papers/<id>` | Paper detail (reading pane) |
| GET | `/search?q=...` | Keyword search |
| POST | `/papers/<id>/fulltext` | Fetch and cache full text |
| GET | `/settings` | Settings form |
| POST | `/settings/save` | Save settings to config |
| POST | `/pipeline/run` | Start async pipeline |
| POST | `/pipeline/resume` | Resume scoring unscored papers |
| GET | `/pipeline/status` | Poll status (returns OOB updates) |
