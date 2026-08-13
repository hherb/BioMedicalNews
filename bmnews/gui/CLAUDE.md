# GUI

Desktop app: pywebview (native window) + Flask (HTTP backend) + HTMX (frontend interactivity).

**Layout:** Email-client style with tab bar, resizable split panes (Split.js), paper list with infinite scroll, reading pane with fulltext toggle, settings form, and pipeline status footer.

**Key features:**
- **HTMX fragment-based updates** — paper list pagination, paper detail, settings, and pipeline status polling (500ms interval) use partial HTML responses
- **Async pipeline execution** — runs in daemon thread with `on_progress` and `on_scored` callbacks; OOB (out-of-band) HTMX swaps update individual paper cards and refresh the list
- **Auto-resume** — on app startup, automatically scores any unscored papers
- **Watches pane** — read-only view of each configured watch with its per-channel
  `delivered / matching / remaining`, plus buttons to deliver one batch or drain the
  queue. Rows are built from `parse_watches()` with the counts joined onto them, so a
  watch that names no configured channel, or that fails to parse, is still shown rather
  than silently absent. A watch whose channel list resolves only *partly* names the
  dropped ones beside its table, and a delivery refused because another job holds the
  lock says so instead of looking like it worked. Which channels a watch resolves to is
  settled from the parsed config, not from the reports, so every one of those notices
  survives a render that skips the counts — which is what `/watches` does while a job
  runs, for the same reason `/watches/rows` answers 204 then. Aggregates across channels
  count **notifications**, not papers: one paper queued for two channels is two
  deliveries, so the drain button carries no total and the terminal status says
  "N notification(s)". Creating and editing watches stays in `config.toml`
- **One background job** — `gui/jobs.py` owns the lock, status and daemon thread that
  the pipeline routes and the watches pane share, so a delivery cannot race a scoring run
- **Fulltext retrieval** — on-demand via `bmlib.fulltext.FullTextService`, seeded with the URLs sync recorded in `fulltext_sources` and falling back through Europe PMC XML, NCBI's own PMC copy, Europe PMC's free PDF, Unpaywall and finally a publisher link from the DOI; JATS XML parsed to HTML; cached in `paper_extras.fulltext_html`. When the text was extracted from a PDF, the reading pane offers **View PDF** alongside it. Every outbound URL passes `_safe_url()` first — these come from upstream services, and escaping stops attribute injection but not a `javascript:` payload
- **Abstract rendering** — `helpers.py` normalises the two shapes an abstract arrives in.
  bioRxiv/medRxiv/Europe PMC send HTML (`<h4>Background</h4>`), which is flattened to
  `Label:` lines and stripped; PubMed, since bmlib 0.8.0, sends Markdown (`**LABEL:**`
  sections, `*em*`/`~sub~`/`^sup^`, prose backslash-escaped over ``\ ` * ~ ^``), whose
  markers become tags. The marker set and both renderings live in `bmnews/markup.py`,
  not here — `db/operations.py` needs them too and must not import from the GUI.
  - **The conversion is gated on the paper's source, and the gate is the whole
    safety argument.** Only PubMed prose is Markdown, and only PubMed prose is
    escaped by bmlib. Run over the others it does not find markup, it invents it:
    `CYP2C19*2 and CYP2C19*17` pairs into `CYP2C19<em>2 and CYP2C19</em>17`, and so do
    `HLA-B*57:01`, `5.9*10(4)`, `~50-~60` and a `(*P<0.05, **P<0.01)` legend. The
    flanking rule (delimiters must hug non-whitespace) narrows that but does not close
    it — all of those hug non-whitespace on both sides, which is what pairing *needs*.
    Measured over a 4,214-abstract corpus, converting ungated changed 18 abstracts, every
    one for the worse. `format_abstract` therefore takes `paper.sources`, and omitting
    the argument converts nothing
  - Markdown converts *after* `escape()` — that call leaves the markers alone, so the
    tags the converter emits are the only unescaped HTML *it* introduces (the `<p>` and
    `<strong>` wrappers are added afterwards by `format_abstract_html` itself).
    Backslash-escaped specials are held aside before any marker matching, or
    `CYP2C19 \*1/\*2` italicises the very star alleles the escaping exists to protect
  - A formatting failure degrades to escaped text with `logger.exception`, never a 500:
    HTMX does not swap on a non-2xx, so an exception here would leave the *previous*
    paper in the pane while the newly clicked card takes the highlight
- **Title rendering** — titles are Markdown too since bmlib 0.8.0, and are flattened to
  text in `_row_to_paper()` rather than by a filter, because every surface renders a
  title as text and because the decode point also reaches user-overridden templates and
  the title the relevance prompt sends to the LLM. Rendering title markup as tags on the
  HTML surfaces is [issue #39](https://github.com/hherb/BioMedicalNews/issues/39)
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
| GET | `/watches` | Watches pane — per-channel delivered/matching/remaining |
| POST | `/watches/<name>/notify` | Deliver one batch (the watch's `max_per_run`) |
| POST | `/watches/<name>/notify-all` | Drain a watch's queue |
| GET | `/watches/rows` | Refresh the counts (204 while a job runs) |
