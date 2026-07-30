# Transparency Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `bmlib.transparency` into bmnews as an inform-only TRANSPARENCY pipeline stage, so a paper's research-integrity assessment is analysed, stored and displayed — replacing a `[transparency]` config section that has silently done nothing since the first release.

**Architecture:** A fifth pipeline stage between SCORE and NOTIFY, query-based and incremental like NOTIFY. It selects scored papers above a combined-score gate, analyses them through one shared `TransparencyAnalyzer` on a small thread pool, and stores each result in a new bmnews-owned `transparency` table. Retries of an indeterminate result are bounded by a stored attempt count. Nothing it produces changes which papers are selected or how they rank — four surfaces display it and no selection query filters on it.

**Tech Stack:** Python 3.11+, `bmlib.transparency` (`TransparencyAnalyzer`, `TransparencyResult`, `TransparencyRisk`, `TransparencySettings`), `bmlib.db` for backend-aware SQL, Click for the CLI, Jinja2 for templates, `concurrent.futures.ThreadPoolExecutor`, pytest.

**Design document:** `docs/plans/2026-07-30-transparency-analysis-design.md` — read it first. Every "why" below is recorded there in more depth.

## Global Constraints

- **Python 3.11+**, `from __future__ import annotations` in every module.
- **ruff**, line-length 100, rules E, F, I, N, W, UP. `uv run ruff check bmnews/ tests/` and `uv run ruff format --check bmnews/ tests/` must both pass.
- **`uv` only** — never invoke `pip` directly.
- **Build against the currently pinned bmlib (0.5.1, commit `7af80d40`).** Every symbol used exists there. Do **not** run `uv lock --upgrade-package bmlib`; the 0.6.0 bump happens after this work lands and requires no code change.
- **Do not import `TransparencyUnknownReason`.** The pinned bmlib does not export it, and the design deliberately does not depend on it.
- **Both database backends.** This plan touches `db/operations.py` and `db/migrations.py`, so `tests/test_db.py` must run against a live PostgreSQL server as well as SQLite. Without `BMNEWS_TEST_PG_DSN` the PostgreSQL half **silently skips**, which is exactly the failure mode to avoid here — a green run then means only that SQLite works.

  **The DSN for this machine** (verified, servers already running — do *not* start a container, and do *not* substitute the `bmnews:bmnews` credentials from CLAUDE.md's example, which is what CI uses; that role does not exist locally):

  ```bash
  # PostgreSQL 16 — the primary target
  BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5432/bmnews_test uv run pytest tests/test_db.py -v
  # PostgreSQL 18 — confirmation pass
  BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5532/bmnews_test uv run pytest tests/test_db.py -v
  ```

  Baseline before this work: **232 passed, 0 skipped** on both. A run reporting skips for `tests/test_db.py` means the DSN did not take — fix that before trusting the result.
- **Keyword-only arguments for writes**, `conn` first, no ORM, `_placeholder(conn)` / `_is_sqlite(conn)` for backend-aware SQL, per-migration DDL pairs.
- **Google-style docstrings** on every public function and class; module-level `logger = logging.getLogger(__name__)`.
- **Inform only.** No task may add a transparency filter to `get_papers_for_digest()`, a criterion to `bmnews/notify/matcher.py`, or apply `tier_downgrade_applied`. Those are explicitly out of scope.
- **No LLM calls and no real HTTP in tests.** `TransparencyAnalyzer.analyze` is always mocked.
- **Commit messages** use conventional style (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `bmnews/transparency/__init__.py` | Package marker; re-exports `run_transparency`, `list_results`, `TransparencyReport`. |
| `bmnews/transparency/service.py` | The stage: select candidates, analyse on a pool, store each result, report. |
| `tests/test_transparency.py` | The service and the CLI command, with the analyzer mocked. |

**Modified:**

| File | Change |
|---|---|
| `bmnews/constants.py` | `TRANSPARENCY_BATCH_SIZE`, `TRANSPARENCY_MAX_ATTEMPTS`. |
| `bmnews/config.py` | `TransparencyConfig` fields + docstring, `_DEPRECATED_KEYS`, `_apply_section`, `DEFAULT_CONFIG_TOML`. |
| `bmnews/metadata.py` | `parse_json_object` primitive; `parse_transparency` alongside `parse_metadata`. |
| `bmnews/db/migrations.py` | Migration 7 DDL pair, `_m007_add_transparency`, `MIGRATIONS` entry. |
| `bmnews/db/operations.py` | `transparency` table CRUD, candidate selection, and the read-path join. |
| `bmnews/pipeline.py` | `_run_transparency_stage()` and its placement in `run_pipeline`. |
| `bmnews/cli.py` | The `bmnews transparency` command. |
| `bmnews/gui/templates/fragments/reading_pane.html` | Risk badge + findings section. |
| `bmnews/gui/static/css/app.css` | `.risk-badge` and friends. |
| `templates/digest_email.html`, `templates/digest_text.txt` | Risk badge / line. |
| `templates/notify_email.html`, `templates/notify_email.txt`, `templates/notify_matrix.html`, `templates/notify_matrix.txt` | Escaped risk badge. |
| `tests/test_config.py`, `tests/test_db.py`, `tests/test_pipeline.py`, `tests/test_digest.py`, `tests/test_gui_app.py`, `tests/test_notify_channels.py` | Coverage for each of the above. |
| `docs/user/*`, `docs/dev/*`, `CLAUDE.md`, `HANDOVER.md` | Documentation. |

---

### Task 1: Config fields, the deprecated-key rename, and constants

**Files:**
- Modify: `bmnews/constants.py` (append a new section at the end)
- Modify: `bmnews/config.py:127-131` (`TransparencyConfig`), `bmnews/config.py:200-207` (`_apply_section`), `bmnews/config.py:437-439` (the `[transparency]` block in `DEFAULT_CONFIG_TOML`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `TransparencyConfig(enabled: bool, min_combined_score: float, score_threshold: int, concurrency: int)`; `constants.TRANSPARENCY_BATCH_SIZE: int = 100`; `constants.TRANSPARENCY_MAX_ATTEMPTS: int = 3`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (it already imports `load_config`, `save_config` and `AppConfig`; add `import logging` and `tomllib` at the top if absent):

```python
def test_transparency_defaults():
    config = AppConfig()
    assert config.transparency.enabled is False
    assert config.transparency.min_combined_score == 0.6
    assert config.transparency.score_threshold == 40
    assert config.transparency.concurrency == 3


def test_renamed_min_score_threshold_carries_forward(tmp_path, caplog):
    """The old key's value is the user's; a rename must not revert it."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[transparency]\nenabled = true\nmin_score_threshold = 0.85\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        config = load_config(path)

    assert config.transparency.min_combined_score == 0.85
    assert "min_score_threshold" in caplog.text
    assert "min_combined_score" in caplog.text


def test_new_key_wins_when_both_are_present(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text(
        "[transparency]\nmin_score_threshold = 0.85\nmin_combined_score = 0.4\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        config = load_config(path)

    assert config.transparency.min_combined_score == 0.4
    assert "obsolete" in caplog.text


def test_transparency_round_trips_through_save(tmp_path):
    """save_config must emit the new key, so the old one disappears on save."""
    config = AppConfig()
    config.transparency.enabled = True
    config.transparency.min_combined_score = 0.75
    config.transparency.score_threshold = 55
    config.transparency.concurrency = 2

    path = save_config(config, tmp_path / "config.toml")
    text = path.read_text(encoding="utf-8")
    assert "min_combined_score = 0.75" in text
    assert "min_score_threshold" not in text

    reloaded = load_config(path)
    assert reloaded.transparency.min_combined_score == 0.75
    assert reloaded.transparency.score_threshold == 55
    assert reloaded.transparency.concurrency == 2


def test_default_config_template_parses_with_the_new_key():
    import tomllib

    from bmnews.config import DEFAULT_CONFIG_TOML

    raw = tomllib.loads(DEFAULT_CONFIG_TOML)
    assert "min_score_threshold" not in raw["transparency"]
    assert raw["transparency"]["min_combined_score"] == 0.6
    assert raw["transparency"]["score_threshold"] == 40
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k transparency -v`
Expected: FAIL — `AttributeError: 'TransparencyConfig' object has no attribute 'min_combined_score'`.

- [ ] **Step 3: Add the constants**

Append to `bmnews/constants.py`:

```python
# --- Transparency -----------------------------------------------------------

#: How many papers one transparency run analyses. bmlib paces every outbound
#: request 0.35 s apart across the whole analyzer, and one analysis makes four
#: to eight of them (CrossRef, a Europe PMC search, its full text, PubMed
#: efetch, OpenAlex, up to three ClinicalTrials.gov lookups) — so 1.4–2.8 s per
#: paper is mandatory however many threads run. Concurrency hides per-request
#: latency; it cannot raise that ceiling. This bounds a run to a few minutes
#: and leaves the rest queued, exactly as ``UNSCORED_BATCH_SIZE`` does.
TRANSPARENCY_BATCH_SIZE: int = 100

#: How many times a paper whose analysis came back UNKNOWN is re-attempted
#: before it is left alone. bmlib sets its reachability flag only on an HTTP
#: 200, so it reports UNREACHABLE both for a network outage and for a paper
#: indexed in none of its five APIs — the two are indistinguishable. Retrying
#: every UNKNOWN forever would therefore re-query every unindexed preprint on
#: every run. ``bmnews transparency --refresh`` resets the count.
TRANSPARENCY_MAX_ATTEMPTS: int = 3
```

- [ ] **Step 4: Replace `TransparencyConfig`**

In `bmnews/config.py`, replace the whole dataclass at lines 126–131:

```python
@dataclass
class TransparencyConfig:
    """Settings for bmlib's research-integrity analysis.

    *Not* scoring rationale, which is what this docstring claimed while nothing
    called the analyzer. :mod:`bmlib.transparency` assesses whether a paper
    discloses its funders, carries a conflict-of-interest statement, makes its
    data available, and — for a registered trial — has posted its results, by
    querying CrossRef, Europe PMC, PubMed, OpenAlex and ClinicalTrials.gov.

    Two fields here are thresholds and they are **not** the same threshold:
    ``min_combined_score`` gates which papers are worth spending requests on (a
    bmnews combined score, 0.0–1.0), while ``score_threshold`` is bmlib's cutoff
    below which a paper's transparency reads as HIGH risk (a transparency score,
    0–100). ``min_combined_score`` was called ``min_score_threshold`` until the
    analyzer was wired up; see :data:`_DEPRECATED_KEYS`.

    ``concurrency`` is separate from ``llm.concurrency`` because the two bound
    different resources — an LLM endpoint and a set of rate-limited public APIs.
    """

    enabled: bool = False
    min_combined_score: float = 0.6
    score_threshold: int = 40
    concurrency: int = 3
```

- [ ] **Step 5: Add the rename map and teach `_apply_section` about it**

In `bmnews/config.py`, immediately above `def _apply_section` (line 200), add:

```python
#: Config keys renamed since a release, per section dataclass: ``{old: new}``.
#: :func:`_apply_section` assigns only to attributes the dataclass already has,
#: so without this a rename would silently discard a value the user had
#: deliberately changed and fall back to the default.
_DEPRECATED_KEYS: dict[type, dict[str, str]] = {
    TransparencyConfig: {"min_score_threshold": "min_combined_score"},
}
```

Then replace `_apply_section` (lines 200–207) with:

```python
def _apply_section(dc: Any, data: dict) -> None:
    """Apply dict values onto a dataclass, ignoring unknown keys.

    A key the dataclass has renamed is carried forward to its new name with a
    warning rather than dropped: the value belongs to the user, and silently
    reverting it to a default is the one outcome a rename must not produce. An
    explicitly set new key always wins, in which case the old one is leftover
    and is reported as such.

    Args:
        dc: The section dataclass instance to populate.
        data: The raw TOML table for that section.
    """
    renames = _DEPRECATED_KEYS.get(type(dc), {})
    for key, value in data.items():
        if key in renames:
            new_key = renames[key]
            if new_key in data:
                logger.warning(
                    "Config key %r is obsolete and %r is also set — using %r. "
                    "Remove the old key from your config file.",
                    key,
                    new_key,
                    new_key,
                )
                continue
            logger.warning(
                "Config key %r has been renamed to %r; carrying the value forward. "
                "Save your settings to update the file.",
                key,
                new_key,
            )
            key = new_key
        if hasattr(dc, key):
            # Backward compat: old configs have research_interests as a list
            if key == "research_interests" and isinstance(value, list):
                value = ", ".join(value)
            setattr(dc, key, value)
```

- [ ] **Step 6: Update the default config template**

In `bmnews/config.py`, replace the `[transparency]` block inside `DEFAULT_CONFIG_TOML` (lines 437–439):

```toml
[transparency]
# Research-integrity analysis: funder disclosure, COI statements, data
# availability and trial-results reporting, via CrossRef, Europe PMC, PubMed,
# OpenAlex and ClinicalTrials.gov. Display only — it never changes which
# papers are selected or how they rank.
enabled = false
# Only analyse papers whose combined score reaches this (0.0-1.0). Each
# analysis costs four to eight external requests, so this is the cost control.
min_combined_score = 0.6
# bmlib's 0-100 cutoff: a transparency score below this reads as HIGH risk.
score_threshold = 40
# Concurrent analyses. bmlib's shared rate limit caps real throughput, so
# raising this hides latency rather than multiplying speed.
concurrency = 3
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, including every pre-existing config test.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/constants.py bmnews/config.py tests/test_config.py
git commit -m "feat(config): give [transparency] real fields and rename its gate

min_score_threshold sat one field away from bmlib's score_threshold while
meaning a different thing on a different scale — ours gates which papers get
analysed (a combined score, 0.0-1.0), bmlib's decides what counts as HIGH
risk (0-100). It is now min_combined_score, carried forward with a warning
through a per-dataclass rename map, because _apply_section assigns only to
attributes that exist and would otherwise have reverted a customised value
to its default without saying so.

The docstring claimed the section configured 'exposing scoring rationale'.
It configures research-integrity analysis and always did."
```

---

### Task 2: Migration 7 and the `transparency` table operations

**Files:**
- Modify: `bmnews/db/migrations.py` (append before the registry at line 773; add the `Migration(7, ...)` entry)
- Modify: `bmnews/db/operations.py` (append a `# --- Transparency ---` section after `count_notifications`, ~line 830)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `constants.TRANSPARENCY_MAX_ATTEMPTS` (Task 1).
- Produces:
  - `save_transparency(conn, *, paper_id: int, transparency_score: int, risk_level: str, result_json: str = "{}", reset_attempts: bool = False) -> None`
  - `get_transparency_candidates(conn, *, min_combined: float = 0.0, limit: int = DEFAULT_QUERY_LIMIT, max_attempts: int = TRANSPARENCY_MAX_ATTEMPTS, refresh: bool = False, paper_id: int | None = None) -> list[dict]` — rows carry `id`, `doi`, `pmid`, `title`, `attempts` (`attempts` is `None` when no result exists yet)
  - `get_transparency_results(conn, *, limit: int = DEFAULT_QUERY_LIMIT) -> list[dict]` — rows carry `paper_id`, `transparency_score`, `risk_level`, `attempts`, `result_json`, `title`, `doi`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`. Add `get_transparency_candidates`, `get_transparency_results` and `save_transparency` to the existing `from bmnews.db.operations import (...)` block. The module already sets `pytestmark = pytest.mark.usefixtures("db_backend")`, so every test below runs once per backend automatically.

```python
class TestTransparency:
    """The transparency table: storage, the attempt ceiling, and selection."""

    def _scored_paper(self, conn, *, doi, combined, pmid=None):
        """Store a paper with a score, returning its publication id."""
        paper_id = store_paper(
            conn,
            doi=doi,
            pmid=pmid,
            title=f"Paper {doi}",
            abstract="Abstract",
            source="medrxiv",
        )
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=combined)
        return paper_id

    def test_save_and_read_back(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)

        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=82,
            risk_level="low",
            result_json='{"transparency_score": 82}',
        )

        rows = get_transparency_results(conn)
        assert len(rows) == 1
        assert rows[0]["paper_id"] == paper_id
        assert rows[0]["transparency_score"] == 82
        assert rows[0]["risk_level"] == "low"
        assert rows[0]["attempts"] == 1
        assert rows[0]["title"] == "Paper 10.1/a"

    def test_repeat_analysis_increments_attempts(self):
        """The ceiling only binds if a repeat actually counts."""
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)

        for _ in range(3):
            save_transparency(
                conn, paper_id=paper_id, transparency_score=0, risk_level="unknown"
            )

        rows = get_transparency_results(conn)
        assert len(rows) == 1, "one row per paper, not one per attempt"
        assert rows[0]["attempts"] == 3

    def test_reset_attempts_restarts_the_budget(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")

        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=0,
            risk_level="unknown",
            reset_attempts=True,
        )

        assert get_transparency_results(conn)[0]["attempts"] == 1

    def test_candidates_respect_the_score_gate(self):
        conn = _db()
        high = self._scored_paper(conn, doi="10.1/high", combined=0.9)
        self._scored_paper(conn, doi="10.1/low", combined=0.1)

        rows = get_transparency_candidates(conn, min_combined=0.5)

        assert [r["id"] for r in rows] == [high]

    def test_unscored_papers_are_never_candidates(self):
        """The gate reads combined_score, so a paper without one cannot pass."""
        conn = _db()
        store_paper(conn, doi="10.1/unscored", title="No score", source="medrxiv")

        assert get_transparency_candidates(conn, min_combined=0.0) == []

    def test_determinate_result_leaves_the_queue(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        assert get_transparency_candidates(conn, min_combined=0.0) == []

    def test_unknown_result_retries_until_the_ceiling(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)

        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        assert [r["id"] for r in get_transparency_candidates(conn, max_attempts=3)] == [paper_id]
        assert get_transparency_candidates(conn, max_attempts=3)[0]["attempts"] == 1

        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")
        save_transparency(conn, paper_id=paper_id, transparency_score=0, risk_level="unknown")

        assert get_transparency_candidates(conn, max_attempts=3) == []

    def test_refresh_reselects_a_determinate_result(self):
        conn = _db()
        paper_id = self._scored_paper(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        rows = get_transparency_candidates(conn, min_combined=0.0, refresh=True)

        assert [r["id"] for r in rows] == [paper_id]

    def test_paper_id_bypasses_the_score_gate(self):
        """The user named this paper; a cost gate for papers nobody reads
        does not apply to one that was asked for by id."""
        conn = _db()
        low = self._scored_paper(conn, doi="10.1/low", combined=0.01)

        rows = get_transparency_candidates(conn, min_combined=0.9, paper_id=low)

        assert [r["id"] for r in rows] == [low]

    def test_candidates_are_ordered_best_score_first(self):
        conn = _db()
        mid = self._scored_paper(conn, doi="10.1/mid", combined=0.6)
        best = self._scored_paper(conn, doi="10.1/best", combined=0.95)

        rows = get_transparency_candidates(conn, min_combined=0.0)

        assert [r["id"] for r in rows] == [best, mid]

    def test_candidates_carry_only_identifying_columns(self):
        """No abstract and no cached full text: this query materialises every
        candidate, so a column it does not need is multiplied by all of them."""
        conn = _db()
        self._scored_paper(conn, doi="10.1/a", pmid="123", combined=0.9)

        row = get_transparency_candidates(conn, min_combined=0.0)[0]

        assert set(row) == {"id", "doi", "pmid", "title", "attempts"}

    def test_results_are_ordered_worst_risk_first(self):
        conn = _db()
        for doi, risk in (("10.1/l", "low"), ("10.1/h", "high"), ("10.1/m", "medium")):
            paper_id = self._scored_paper(conn, doi=doi, combined=0.9)
            save_transparency(conn, paper_id=paper_id, transparency_score=50, risk_level=risk)

        assert [r["risk_level"] for r in get_transparency_results(conn)] == [
            "high",
            "medium",
            "low",
        ]


class TestMigration7:
    def test_creates_the_transparency_table(self):
        # new_db(), not _db(): the latter calls init_db(), which runs *every*
        # migration, so the table would already exist and the assertion below
        # could never fail. This matches the file's own _v3_db()/_v5_db().
        conn = new_db()
        run_migrations(conn, MIGRATIONS[:6])
        assert not table_exists(conn, "transparency")

        run_migrations(conn, MIGRATIONS)

        assert table_exists(conn, "transparency")

    def test_is_idempotent(self):
        conn = _db()
        init_db(conn)
        run_migrations(conn, MIGRATIONS)
        assert table_exists(conn, "transparency")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_db.py -k "Transparency or Migration7" -v`
Expected: FAIL — `ImportError: cannot import name 'save_transparency'`.

- [ ] **Step 3: Add migration 7**

In `bmnews/db/migrations.py`, insert immediately before the `# Migration registry` banner at line 773:

```python
# ---------------------------------------------------------------------------
# Migration 7: transparency analysis results
# ---------------------------------------------------------------------------

# bmnews-owned, one row per publication. ``paper_id`` is the primary key rather
# than a surrogate id with UNIQUE(paper_id): there is exactly one result per
# paper, and it gives the upsert its conflict target for free.
#
# ``result_json`` holds bmlib's whole ``TransparencyResult.to_dict()`` the way
# ``scores.assessment_json`` holds a quality assessment, so a field bmlib adds
# later — ``unknown_reason``, for one — needs no migration to start being
# stored.
#
# ``attempts`` is what stops an unanalysable paper being re-queried forever.
# bmlib sets its reachability flag only on an HTTP 200, so it reports
# UNREACHABLE both for a network outage and for a paper indexed in none of its
# five APIs; the two cannot be told apart, so "retry every UNKNOWN" has no
# natural end. ``get_transparency_candidates`` retries only while this stays
# under ``TRANSPARENCY_MAX_ATTEMPTS``.
_M007_SQLITE = """\
CREATE TABLE IF NOT EXISTS transparency (
    paper_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    transparency_score INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    result_json TEXT NOT NULL DEFAULT '{}',
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transparency_risk ON transparency (risk_level);
"""

_M007_POSTGRESQL = """\
CREATE TABLE IF NOT EXISTS transparency (
    paper_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    transparency_score INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    result_json TEXT NOT NULL DEFAULT '{}',
    analyzed_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transparency_risk ON transparency (risk_level);
"""


def _m007_add_transparency(conn: Any) -> None:
    """Create the ``transparency`` table holding bmlib's analysis results."""
    create_tables(conn, _M007_SQLITE if _is_sqlite(conn) else _M007_POSTGRESQL)
```

Then append to `MIGRATIONS`:

```python
    Migration(7, "add_transparency", _m007_add_transparency),
```

> **Superseded during review.** The `idx_transparency_risk` index in both DDL
> strings above was dropped before merge; migration 7 ships without it. Neither
> query that mentions `risk_level` can use it — the candidate query reaches the
> row through `paper_id`, and `get_transparency_results()` sorts on a CASE
> expression — so it would have cost a write on every upsert and been read by
> nothing.

- [ ] **Step 4: Add the operations**

In `bmnews/db/operations.py`, add `TRANSPARENCY_MAX_ATTEMPTS` to the `from bmnews.constants import (...)` block, then append after `count_notifications` (before `# --- Paper extras ...` at line 832):

```python
# --- Transparency ---


def save_transparency(
    conn: Any,
    *,
    paper_id: int,
    transparency_score: int,
    risk_level: str,
    result_json: str = "{}",
    reset_attempts: bool = False,
) -> None:
    """Insert or update one paper's transparency result.

    A repeat analysis **increments** ``attempts``, which is what bounds the
    retry of an indeterminate result. Without the increment the ceiling never
    binds and a paper indexed in none of bmlib's APIs is re-queried on every
    run forever, since bmlib cannot distinguish that from a network outage.

    Args:
        conn: DB-API connection.
        paper_id: The ``publications`` row this result is for.
        transparency_score: bmlib's 0–100 transparency score.
        risk_level: ``low``, ``medium``, ``high`` or ``unknown`` — a
            :class:`bmlib.transparency.TransparencyRisk` value.
        result_json: bmlib's whole ``TransparencyResult.to_dict()``, encoded.
        reset_attempts: Restart the retry budget at 1 instead of incrementing.
            Set by ``--refresh``: an explicit re-analysis is the user asking
            for the work again, so it must restore the automatic retries too.
            Otherwise a refreshed paper would spend its whole budget on that
            single attempt and a transient outage would strand it as UNKNOWN.
    """
    ph = _placeholder(conn)
    sqlite = _is_sqlite(conn)
    now = "datetime('now')" if sqlite else "NOW()"
    excluded = "excluded" if sqlite else "EXCLUDED"
    # Qualifying the existing row by table name works on both backends, as
    # ``record_notification`` already relies on.
    attempts = "1" if reset_attempts else "transparency.attempts + 1"

    sql = f"""
        INSERT INTO transparency (paper_id, transparency_score, risk_level, result_json)
        VALUES ({ph}, {ph}, {ph}, {ph})
        ON CONFLICT(paper_id) DO UPDATE SET
            transparency_score = {excluded}.transparency_score,
            risk_level = {excluded}.risk_level,
            result_json = {excluded}.result_json,
            attempts = {attempts},
            analyzed_at = {now}
    """

    with _transaction(conn):
        execute(conn, sql, (paper_id, transparency_score, risk_level, result_json))


# Enough to identify a paper to bmlib's analyzer and to report on it. The
# analyzer takes identifiers only, so the abstract, the author list and the
# GUI's cached full text are all dead weight in a query that materialises every
# candidate — the same lesson `_NOTIFY_PAPER_COLUMNS` records. ``t.attempts``
# rides along so the caller knows the retry budget it is about to spend without
# reading the row back after writing it.
_TRANSPARENCY_CANDIDATE_COLUMNS = """
    p.id, p.doi, p.pmid, p.title, t.attempts
"""


def get_transparency_candidates(
    conn: Any,
    *,
    min_combined: float = 0.0,
    limit: int = DEFAULT_QUERY_LIMIT,
    max_attempts: int = TRANSPARENCY_MAX_ATTEMPTS,
    refresh: bool = False,
    paper_id: int | None = None,
) -> list[dict]:
    """Select scored papers whose transparency is still worth analysing.

    The queue is *papers with no result yet*, plus results that came back
    ``unknown`` and have not spent their attempts. A determinate result never
    returns however high its ``attempts`` climbed, because the ``risk_level``
    test fails first.

    Args:
        conn: DB-API connection.
        min_combined: Floor on the combined score — the cost gate, since each
            analysis spends four to eight external requests. Ignored when
            *paper_id* names a paper.
        limit: Maximum rows to return.
        max_attempts: How many times an ``unknown`` result may be re-attempted.
        refresh: Also select papers already holding a determinate result.
        paper_id: Restrict to one publication and skip the score gate — the
            user named that paper, so a gate meant to avoid spending requests
            on papers nobody will read does not apply to it.

    Returns:
        Rows carrying ``id``, ``doi``, ``pmid``, ``title`` and ``attempts``
        (``None`` when no result exists yet), best combined score first.
    """
    ph = _placeholder(conn)
    params: list = []

    # Every publication has a DOI or a PMID — migration 4 could not represent a
    # row with neither — so this is defensive. It is also what guarantees
    # bmlib's NO_IDENTIFIER result is never reached, and therefore that the
    # attempt count never has to tell a permanent failure from a transient one.
    conditions = ["(p.doi IS NOT NULL OR p.pmid IS NOT NULL)"]

    if paper_id is not None:
        conditions.append(f"p.id = {ph}")
        params.append(paper_id)
    else:
        conditions.append(f"s.combined_score >= {ph}")
        params.append(min_combined)

    if not refresh:
        conditions.append(
            f"(t.paper_id IS NULL OR (t.risk_level = 'unknown' AND t.attempts < {ph}))"
        )
        params.append(max_attempts)

    params.append(limit)

    rows = fetch_all(
        conn,
        f"""
        SELECT {_TRANSPARENCY_CANDIDATE_COLUMNS}
        FROM publications p
        JOIN scores s ON s.paper_id = p.id
        LEFT JOIN transparency t ON t.paper_id = p.id
        WHERE {" AND ".join(conditions)}
        ORDER BY s.combined_score DESC, p.id ASC
        LIMIT {ph}
        """,
        tuple(params),
    )
    return [dict(row) for row in rows]
```

> **Superseded during review.** The single `ORDER BY` above shipped as two.
> Score order is right for the normal queue, which narrows itself — a paper
> drops out once it holds a result. A refresh run has no such predicate, so
> score order returned the identical top-`limit` papers on every run and never
> reached the rest of the corpus. The merged version switches to
> `t.analyzed_at ASC NULLS FIRST, s.combined_score DESC, p.id ASC` when
> `refresh` is set. `NULLS FIRST` is explicit because SQLite sorts NULLs first
> in `ASC` and PostgreSQL sorts them last.

```python
def get_transparency_results(conn: Any, *, limit: int = DEFAULT_QUERY_LIMIT) -> list[dict]:
    """Read stored transparency results, worst risk first.

    Ordered by risk rather than score so ``bmnews transparency --list`` opens
    on the papers that warrant a second look. The ranking is spelled out as a
    CASE rather than leaning on the alphabetical accident that ``high`` sorts
    before ``low`` before ``medium``.

    Args:
        conn: DB-API connection.
        limit: Maximum rows to return.

    Returns:
        Rows carrying ``paper_id``, ``transparency_score``, ``risk_level``,
        ``attempts``, ``result_json``, ``title`` and ``doi``.
    """
    ph = _placeholder(conn)
    rows = fetch_all(
        conn,
        f"""
        SELECT t.paper_id, t.transparency_score, t.risk_level, t.attempts,
               t.result_json, p.title, p.doi
        FROM transparency t
        JOIN publications p ON p.id = t.paper_id
        ORDER BY CASE t.risk_level
                     WHEN 'high' THEN 0
                     WHEN 'medium' THEN 1
                     WHEN 'low' THEN 2
                     ELSE 3
                 END,
                 t.transparency_score ASC, t.paper_id ASC
        LIMIT {ph}
        """,
        (limit,),
    )
    return [dict(row) for row in rows]
```

- [ ] **Step 5: Run the tests on SQLite**

Run: `uv run pytest tests/test_db.py -k "Transparency or Migration7" -v`
Expected: PASS on the `sqlite` parameterisation, SKIP on `postgresql`.

- [ ] **Step 6: Run the tests on PostgreSQL**

This step is not optional — the DDL pair and the upsert's `EXCLUDED` casing are backend-specific and wholly untested by SQLite. Both servers are already running; see Global Constraints for why the DSN is not the one in CLAUDE.md.

```bash
BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5432/bmnews_test uv run pytest tests/test_db.py -v
BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5532/bmnews_test uv run pytest tests/test_db.py -v
```

Expected: PASS on both parameterisations with **no skips**, on both PostgreSQL 16 and 18. A skip means the DSN did not take.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/db/migrations.py bmnews/db/operations.py tests/test_db.py
git commit -m "feat(db): store transparency results, with a bounded retry

Migration 7 adds a bmnews-owned transparency table, one row per publication,
holding bmlib's whole TransparencyResult.to_dict() the way scores holds a
quality assessment — so a field bmlib adds later needs no migration.

The attempts column is the load-bearing part. bmlib sets its reachability
flag only on an HTTP 200, so it reports UNREACHABLE both for a network
outage and for a paper indexed in none of its five APIs. The two are
indistinguishable, so retrying every UNKNOWN has no natural end and would
re-query every unindexed preprint on every run. Selection retries only
while attempts stays under the ceiling; --refresh resets it, because an
explicit re-analysis has to restore the automatic retries as well.

Tested on both backends."
```

---

### Task 3: The read path — join transparency onto every paper query

**Files:**
- Modify: `bmnews/metadata.py`
- Modify: `bmnews/db/operations.py:42-54` (`_PAPER_COLUMNS`, `_PAPER_FROM`), `:196-201` (`get_paper_with_score`), `:596-599` (`_NOTIFY_PAPER_COLUMNS`), `:658-677` (the candidate query's FROM), `:1014` (`_NULLABLE_TEXT_COLUMNS`), `:1067` (`_row_to_paper`)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: the `transparency` table (Task 2).
- Produces: every paper dict gains `transparency_risk: str` (`""` when unanalysed) and `transparency_score: int | None`; `get_paper_with_score()` additionally yields `transparency: dict`. New `metadata.parse_transparency(raw) -> dict` and `metadata.parse_json_object(raw) -> dict`.

> **Why this is small:** every use of `_PAPER_COLUMNS` is paired with `_PAPER_FROM` (`get_papers_filtered` via its `base_query`), so adding the join in one place and the columns in another reaches all eight paper queries without touching them individually.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
class TestTransparencyReadPath:
    def test_digest_papers_carry_the_risk_badge(self):
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        papers = get_papers_for_digest(conn, min_combined=0.0)

        assert papers[0]["transparency_risk"] == "low"
        assert papers[0]["transparency_score"] == 82

    def test_unanalysed_paper_reads_as_empty_not_none(self):
        """Templates guard on truthiness, exactly as they do for quality_tier."""
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)

        papers = get_papers_for_digest(conn, min_combined=0.0)

        assert papers[0]["transparency_risk"] == ""

    def test_detail_query_decodes_the_result_blob(self):
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_transparency(
            conn,
            paper_id=paper_id,
            transparency_score=30,
            risk_level="high",
            result_json='{"risk_indicators": ["No COI disclosure found in full text"]}',
        )

        paper = get_paper_with_score(conn, paper_id)

        assert paper["transparency"]["risk_indicators"] == [
            "No COI disclosure found in full text"
        ]

    def test_detail_query_survives_a_malformed_blob(self):
        """A display surface must not fail to render because of stored junk."""
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_transparency(
            conn, paper_id=paper_id, transparency_score=0, risk_level="unknown",
            result_json="not json at all",
        )

        assert get_paper_with_score(conn, paper_id)["transparency"] == {}

    def test_list_queries_do_not_carry_the_blob(self):
        """Absent means 'not asked for', which must not read as 'analysed and
        empty' — so only the detail query populates it."""
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        papers = get_papers_for_digest(conn, min_combined=0.0)

        assert "transparency" not in papers[0]

    def test_notification_candidates_carry_the_badge_but_not_the_blob(self):
        conn = _db()
        paper_id = store_paper(conn, doi="10.1/a", title="A", abstract="x", source="medrxiv")
        save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=82, risk_level="low")

        papers = get_notification_candidates(conn, watch="w", channel="c")

        assert papers[0]["transparency_risk"] == "low"
        assert "transparency" not in papers[0]
        assert "fulltext_html" not in papers[0]
```

And to `tests/test_config.py` — no; the decoder belongs with the DB tests above. Additionally add a focused unit test in `tests/test_db.py` for the decoder itself is unnecessary; `parse_transparency` is exercised by the two blob tests.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_db.py -k TransparencyReadPath -v`
Expected: FAIL — `KeyError: 'transparency_risk'`.

- [ ] **Step 3: Refactor `metadata.py` and add `parse_transparency`**

Replace the body of `bmnews/metadata.py` below its module docstring (keep the docstring, widening its first line to "Helpers for the JSON-object columns bmnews stores."):

```python
from __future__ import annotations

import json
from typing import Any

__all__ = ["parse_json_object", "parse_metadata", "parse_transparency"]


def parse_json_object(raw: Any) -> dict:
    """Decode a stored JSON-object column into a dict.

    Args:
        raw: The stored value — a JSON string, an already-decoded dict, or None.

    Returns:
        The decoded mapping, or an empty dict when it is missing, malformed, or
        not a JSON object.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_metadata(raw: Any) -> dict:
    """Decode a stored ``paper_extras.metadata_json`` value into a dict.

    The column holds data written by every fetcher and by earlier versions of
    the app, so a malformed or unexpectedly shaped value degrades to "no
    metadata" rather than raising.
    """
    return parse_json_object(raw)


def parse_transparency(raw: Any) -> dict:
    """Decode a stored ``transparency.result_json`` value into a dict.

    Deliberately **not** ``bmlib.transparency.TransparencyResult.from_dict()``.
    That classmethod raises on an ``unknown_reason`` value it does not
    recognise, which is right for bmlib and wrong here: a newer bmlib writing a
    member this one has not heard of must not stop a paper's page rendering.
    The display surfaces read plain keys, so a dict is all they need.
    """
    return parse_json_object(raw)
```

- [ ] **Step 4: Join transparency into the shared query fragments**

In `bmnews/db/operations.py`, replace lines 42–54:

```python
# Every paper query selects the same join, so the SELECT list and the FROM
# clause live here rather than being retyped (and drifting) per query. The
# transparency columns ride along on both, which is what makes the risk badge
# available to the digest, the GUI and the tag views without editing eight
# queries. Only ``get_paper_with_score`` adds ``result_json`` on top.
_PAPER_COLUMNS = """
    p.*, e.metadata_json, e.fulltext_html, e.fulltext_source, e.fulltext_pdf_url,
    t.risk_level AS transparency_risk, t.transparency_score
"""

_SCORE_COLUMNS = """
    s.relevance_score, s.quality_score, s.combined_score,
    s.summary, s.study_design, s.quality_tier
"""

_PAPER_FROM = """
    FROM publications p
    LEFT JOIN paper_extras e ON e.publication_id = p.id
    LEFT JOIN transparency t ON t.paper_id = p.id
"""
```

Then update the import of `parse_metadata` at line 33 to:

```python
from bmnews.metadata import parse_metadata, parse_transparency
```

- [ ] **Step 5: Select the blob in the detail query only**

In `get_paper_with_score` (line 197), change the SELECT list:

```python
        SELECT {_PAPER_COLUMNS}, {_SCORE_COLUMNS}, s.assessment_json,
               t.result_json AS transparency_json
```

- [ ] **Step 6: Add the badge to the notification scan**

Replace `_NOTIFY_PAPER_COLUMNS` (lines 596–599), keeping the comment above it intact and appending to it:

```python
_NOTIFY_PAPER_COLUMNS = """
    p.id, p.doi, p.pmid, p.pmcid, p.title, p.abstract, p.journal,
    p.publication_date, p.authors, p.sources,
    t.risk_level AS transparency_risk, t.transparency_score
"""
```

Extend that block's existing comment with one sentence:

```python
# The two transparency columns are an integer and a short enum value, so they
# cost nothing here; ``result_json`` is deliberately left out for the same
# reason the cached full text is.
```

And in `get_notification_candidates`, add the join to its FROM (after the `scores` join at line 663):

```python
        FROM publications p
        JOIN scores s ON s.paper_id = p.id
        LEFT JOIN transparency t ON t.paper_id = p.id
        LEFT JOIN notifications n
```

- [ ] **Step 7: Decode both new shapes in `_row_to_paper`**

Add `"transparency_risk"` to `_NULLABLE_TEXT_COLUMNS` (line 1014):

```python
_NULLABLE_TEXT_COLUMNS = ("abstract", "journal", "license", "transparency_risk")
```

and in `_row_to_paper`, after the `metadata` line (1067):

```python
    paper["metadata"] = parse_metadata(paper.get("metadata_json"))
    # Only the paper-detail query selects the blob. Its absence therefore means
    # "not asked for", which must not decode to the same empty dict as
    # "analysed, nothing to report" — hence the membership test rather than
    # an unconditional ``.get()``.
    if "transparency_json" in paper:
        paper["transparency"] = parse_transparency(paper["transparency_json"])
```

Also extend the `_row_to_paper` docstring's summary of what it normalises:

```python
    """Convert a joined DB row into the paper dict the rest of bmnews uses.

    The JSON array columns become real lists, the source-specific extras blob
    becomes a ``metadata`` dict, the transparency result blob becomes a
    ``transparency`` dict when the query asked for it, and the outbound ``url``
    is derived from the identifiers.
    """
```

- [ ] **Step 8: Run the full DB suite on both backends**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (sqlite), SKIP (postgresql).

Run: `BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5432/bmnews_test uv run pytest tests/test_db.py -v
BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5532/bmnews_test uv run pytest tests/test_db.py -v`
Expected: PASS on both.

Then run the whole suite, because `_PAPER_COLUMNS` feeds the GUI and digest tests too:

Run: `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/metadata.py bmnews/db/operations.py tests/test_db.py
git commit -m "feat(db): carry the transparency badge on every paper query

_PAPER_COLUMNS and _PAPER_FROM are shared by all eight paper queries, so
joining transparency there reaches the digest, the GUI and the tag views at
once. get_paper_with_score adds result_json on top, because the reading pane
is the only surface that renders the findings; the notification scan gets the
two small columns and not the blob, for the reason already recorded there
about the cached full text.

transparency_risk joins _NULLABLE_TEXT_COLUMNS so a LEFT JOIN miss decodes
to '' — an unanalysed paper then renders as nothing at all, exactly as
quality_tier and study_design already do, and no template needs a
'not yet analysed' branch.

The blob is decoded with a plain JSON parse rather than
TransparencyResult.from_dict(), which raises on an unknown_reason it does not
recognise. Correct for bmlib; wrong for a page that has to render."
```

---

### Task 4: The transparency service

**Files:**
- Create: `bmnews/transparency/__init__.py`, `bmnews/transparency/service.py`
- Create: `tests/test_transparency.py`

**Interfaces:**
- Consumes: `TransparencyConfig` (Task 1); `save_transparency`, `get_transparency_candidates`, `get_transparency_results` (Task 2); `TRANSPARENCY_BATCH_SIZE`, `TRANSPARENCY_MAX_ATTEMPTS` (Task 1).
- Produces:
  - `TransparencyReport(candidates: int, analyzed: int, indeterminate: int, exhausted: int, failed: int)` — all defaulting to 0, frozen dataclass
  - `run_transparency(config, *, refresh=False, paper_id=None, limit=None, dry_run=False, on_progress=None) -> TransparencyReport`
  - `list_results(config, *, limit=None) -> list[dict]`
  - `build_settings(config) -> TransparencySettings`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transparency.py`:

```python
"""Tests for the transparency stage.

``TransparencyAnalyzer.analyze`` is always mocked: the real one makes four to
eight requests to CrossRef, Europe PMC, PubMed, OpenAlex and
ClinicalTrials.gov per paper.
"""

from __future__ import annotations

import json

import pytest
from bmlib.db import execute, placeholder
from bmlib.transparency import TransparencyResult, TransparencyRisk

from bmnews.config import AppConfig
from bmnews.db.operations import (
    get_transparency_results,
    save_score,
    save_transparency,
    store_paper,
)
from bmnews.db.schema import init_db
from bmnews.transparency import service


def _result(paper_id, *, score=80, risk=TransparencyRisk.LOW, indicators=()):
    """Build a bmlib result the way the analyzer would return one."""
    return TransparencyResult(
        document_id=str(paper_id),
        transparency_score=score,
        risk_level=risk,
        risk_indicators=list(indicators),
    )


class _FakeAnalyzer:
    """Stands in for bmlib's analyzer, recording what it was asked."""

    def __init__(self, results=None, *, raises=()):
        self.results = results or {}
        self.raises = set(raises)
        self.calls = []

    def analyze(self, document_id, *, pmid=None, doi=None):
        self.calls.append((document_id, pmid, doi))
        if document_id in self.raises:
            raise RuntimeError("CrossRef exploded")
        return self.results.get(document_id) or _result(document_id)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A migrated file-backed database the service will reopen for itself."""
    path = tmp_path / "bmnews.db"
    config = AppConfig()
    config.database.sqlite_path = str(path)
    config.transparency.enabled = True
    config.transparency.min_combined_score = 0.5

    from bmnews.db.schema import open_db

    conn = open_db(config)
    init_db(conn)
    yield config, conn
    conn.close()


def _scored(conn, *, doi, combined, pmid=None):
    paper_id = store_paper(
        conn, doi=doi, pmid=pmid, title=f"Paper {doi}", abstract="Abstract", source="medrxiv"
    )
    save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=combined)
    return paper_id


def _install(monkeypatch, analyzer):
    monkeypatch.setattr(service, "TransparencyAnalyzer", lambda **kwargs: analyzer)


class TestRunTransparency:
    def test_disabled_config_builds_no_analyzer(self, db, monkeypatch):
        """bmlib answers a disabled analyze() with an UNKNOWN placeholder, and
        storing one would satisfy the 'no row yet' half of the candidate query
        — so the paper would never be analysed once the feature was enabled."""
        config, conn = db
        config.transparency.enabled = False
        _scored(conn, doi="10.1/a", combined=0.9)

        def _boom(**kwargs):
            raise AssertionError("analyzer must not be constructed when disabled")

        monkeypatch.setattr(service, "TransparencyAnalyzer", _boom)

        report = service.run_transparency(config)

        assert report == service.TransparencyReport()
        assert get_transparency_results(conn) == []

    def test_analyses_and_stores_a_candidate(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        analyzer = _FakeAnalyzer({str(paper_id): _result(paper_id, score=82)})
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert report.analyzed == 1
        assert report.indeterminate == 0
        rows = get_transparency_results(conn)
        assert rows[0]["transparency_score"] == 82
        assert rows[0]["risk_level"] == "low"
        assert json.loads(rows[0]["result_json"])["transparency_score"] == 82

    def test_passes_both_identifiers_to_the_analyzer(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", pmid="99", combined=0.9)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        service.run_transparency(config)

        assert analyzer.calls == [(str(paper_id), "99", "10.1/a")]

    def test_skips_papers_below_the_gate(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/low", combined=0.1)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert report.analyzed == 0
        assert analyzer.calls == []

    def test_dry_run_counts_without_analysing(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/a", combined=0.9)

        def _boom(**kwargs):
            raise AssertionError("dry run must not construct an analyzer")

        monkeypatch.setattr(service, "TransparencyAnalyzer", _boom)

        report = service.run_transparency(config, dry_run=True)

        assert report.candidates == 1
        assert report.analyzed == 0
        assert get_transparency_results(conn) == []

    def test_a_raising_analysis_costs_only_itself(self, db, monkeypatch):
        config, conn = db
        bad = _scored(conn, doi="10.1/bad", combined=0.9)
        good = _scored(conn, doi="10.1/good", combined=0.8)
        analyzer = _FakeAnalyzer(raises=[str(bad)])
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert report.failed == 1
        assert report.analyzed == 1
        assert [r["paper_id"] for r in get_transparency_results(conn)] == [good]

    def test_a_failed_analysis_leaves_no_row_so_it_retries(self, db, monkeypatch):
        config, conn = db
        bad = _scored(conn, doi="10.1/bad", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer(raises=[str(bad)]))

        service.run_transparency(config)

        assert get_transparency_results(conn) == []

    def test_unknown_result_is_reported_indeterminate(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        analyzer = _FakeAnalyzer(
            {str(paper_id): _result(paper_id, score=0, risk=TransparencyRisk.UNKNOWN)}
        )
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config)

        assert (report.analyzed, report.indeterminate, report.exhausted) == (1, 1, 0)

    def test_reaching_the_attempt_ceiling_is_reported_exhausted(self, db, monkeypatch):
        """The only outcome the user cannot fix by waiting, so it is counted."""
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        analyzer = _FakeAnalyzer(
            {str(paper_id): _result(paper_id, score=0, risk=TransparencyRisk.UNKNOWN)}
        )
        _install(monkeypatch, analyzer)

        for _ in range(3):
            report = service.run_transparency(config)

        assert report.exhausted == 1
        assert service.run_transparency(config).analyzed == 0, "queue is spent"

    def test_refresh_reanalyses_and_resets_the_budget(self, db, monkeypatch):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        save_transparency(
            conn, paper_id=paper_id, transparency_score=0, risk_level="unknown"
        )
        save_transparency(
            conn, paper_id=paper_id, transparency_score=0, risk_level="unknown"
        )
        save_transparency(
            conn, paper_id=paper_id, transparency_score=0, risk_level="unknown"
        )
        assert service.run_transparency(config).analyzed == 0

        _install(monkeypatch, _FakeAnalyzer({str(paper_id): _result(paper_id, score=70)}))
        report = service.run_transparency(config, refresh=True)

        assert report.analyzed == 1
        rows = get_transparency_results(conn)
        assert rows[0]["attempts"] == 1
        assert rows[0]["risk_level"] == "low"

    def test_paper_id_analyses_below_the_gate(self, db, monkeypatch):
        config, conn = db
        low = _scored(conn, doi="10.1/low", combined=0.01)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config, paper_id=low)

        assert report.analyzed == 1
        assert analyzer.calls == [(str(low), None, "10.1/low")]

    def test_limit_caps_the_batch(self, db, monkeypatch):
        config, conn = db
        for i in range(4):
            _scored(conn, doi=f"10.1/{i}", combined=0.9 - i * 0.01)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        report = service.run_transparency(config, limit=2)

        assert report.analyzed == 2
        assert len(analyzer.calls) == 2

    def test_a_full_batch_warns_that_more_remain(self, db, monkeypatch, caplog):
        config, conn = db
        for i in range(3):
            _scored(conn, doi=f"10.1/{i}", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())

        with caplog.at_level("WARNING"):
            service.run_transparency(config, limit=3)

        assert "more" in caplog.text.lower()

    def test_progress_is_reported(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/a", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())
        messages = []

        service.run_transparency(config, on_progress=messages.append)

        assert any("ransparency" in m for m in messages)

    def test_concurrency_greater_than_one_stores_every_result(self, db, monkeypatch):
        """Storage happens on the calling thread; a worker must never touch
        the connection."""
        config, conn = db
        config.transparency.concurrency = 4
        for i in range(6):
            _scored(conn, doi=f"10.1/{i}", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())

        report = service.run_transparency(config)

        assert report.analyzed == 6
        assert len(get_transparency_results(conn)) == 6


class TestBuildSettings:
    def test_enabled_is_forced_true(self, db):
        config, _ = db
        assert service.build_settings(config).enabled is True

    def test_score_threshold_and_concurrency_are_passed_through(self, db):
        config, _ = db
        config.transparency.score_threshold = 55
        config.transparency.concurrency = 7

        settings = service.build_settings(config)

        assert settings.score_threshold == 55
        assert settings.max_concurrent_analyses == 7

    def test_downgrade_flags_keep_bmlib_defaults(self, db):
        """They feed calculate_risk_level, not only the tier downgrade this
        stage ignores — so they shape the badge we display."""
        config, _ = db
        settings = service.build_settings(config)

        assert settings.industry_funding_triggers_downgrade is True
        assert settings.missing_coi_triggers_downgrade is True

    def test_filtering_stays_off(self, db):
        """Caller-honoured, and this caller does not filter."""
        config, _ = db
        assert service.build_settings(config).filtering_enabled is False


class TestListResults:
    def test_lists_stored_results(self, db):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        save_transparency(conn, paper_id=paper_id, transparency_score=20, risk_level="high")

        rows = service.list_results(config)

        assert rows[0]["risk_level"] == "high"
        assert rows[0]["title"] == "Paper 10.1/a"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transparency.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bmnews.transparency'`.

- [ ] **Step 3: Create the package marker**

Create `bmnews/transparency/__init__.py`:

```python
"""Research-integrity analysis for stored papers.

Wraps :mod:`bmlib.transparency` as a pipeline stage. Named alongside it without
ambiguity because every import in bmnews is absolute.
"""

from __future__ import annotations

from bmnews.transparency.service import (
    TransparencyReport,
    build_settings,
    list_results,
    run_transparency,
)

__all__ = [
    "TransparencyReport",
    "build_settings",
    "list_results",
    "run_transparency",
]
```

- [ ] **Step 4: Write the service**

Create `bmnews/transparency/service.py`:

```python
"""The transparency stage: select, analyse, store.

Sits between scoring and the notifications as a fifth stage, and like the
notify stage it is **query-based**: it asks which scored papers still want a
result rather than being driven by a per-paper callback, so it survives a crash
mid-run and tests without running the scorer at all.

It **informs only**. A result is displayed beside a paper and never changes
which papers are selected or how they rank — bmlib's ``tier_downgrade_applied``
is stored and not applied. A value derived from five external APIs must not be
able to move a ``combined_score`` the user has already acted on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from bmlib.transparency import TransparencyAnalyzer, TransparencyRisk, TransparencySettings

from bmnews.config import AppConfig
from bmnews.constants import (
    DEFAULT_CONTACT_EMAIL,
    TRANSPARENCY_BATCH_SIZE,
    TRANSPARENCY_MAX_ATTEMPTS,
)
from bmnews.db.operations import (
    get_transparency_candidates,
    get_transparency_results,
    save_transparency,
)
from bmnews.db.schema import init_db, open_db

logger = logging.getLogger(__name__)

#: bmlib's own string for an indeterminate result, read from the enum rather
#: than spelled out so a rename upstream cannot silently stop matching.
_UNKNOWN = TransparencyRisk.UNKNOWN.value


@dataclass(frozen=True)
class TransparencyReport:
    """What one transparency run did.

    ``indeterminate`` and ``exhausted`` are nested subsets of ``analyzed``, not
    disjoint buckets: ``analyzed - indeterminate`` is how many papers were
    actually assessed, and ``exhausted`` is how many of the remainder will never
    be attempted again without ``--refresh``. That last number is the one worth
    surfacing, because it is the only outcome waiting will not fix.

    Attributes:
        candidates: Papers selected. The only field a dry run fills.
        analyzed: Results stored, determinate or not.
        indeterminate: Subset of ``analyzed`` that came back UNKNOWN.
        exhausted: Subset of ``indeterminate`` now at the attempt ceiling.
        failed: Analyses that raised. No row was written, so they retry.
    """

    candidates: int = 0
    analyzed: int = 0
    indeterminate: int = 0
    exhausted: int = 0
    failed: int = 0


def build_settings(config: AppConfig) -> TransparencySettings:
    """Build bmlib's settings object from bmnews's four config fields.

    ``enabled`` is hard-coded ``True`` because :func:`run_transparency` returns
    before reaching here when the feature is off. Passing the config value
    through would be worse than redundant: bmlib answers a disabled
    ``analyze()`` with an UNKNOWN placeholder, and storing one both reads as a
    finding and satisfies the "no row yet" half of the candidate query, so the
    paper would never be analysed once the feature was switched on.

    ``industry_funding_triggers_downgrade`` and
    ``missing_coi_triggers_downgrade`` keep bmlib's defaults deliberately. They
    are not only about the tier downgrade this stage ignores — they feed
    ``calculate_risk_level()``, so they are what makes an industry-funded paper
    with restricted data read HIGH rather than MEDIUM. ``filtering_enabled``
    stays false because this caller does not filter, and the settings object
    should not claim otherwise.

    Args:
        config: Application config.

    Returns:
        Settings for a :class:`~bmlib.transparency.TransparencyAnalyzer`.
    """
    return TransparencySettings(
        enabled=True,
        score_threshold=config.transparency.score_threshold,
        max_concurrent_analyses=config.transparency.concurrency,
    )


def _build_analyzer(config: AppConfig) -> TransparencyAnalyzer:
    """Construct the analyzer, reusing whatever contact details config holds.

    The PubMed API key is read from the source options rather than duplicated
    into ``[transparency]``: it is the same NCBI credential the PubMed fetcher
    already takes, and sending it moves bmlib's ``efetch`` traffic out of the
    per-IP rate bucket that bmnews's own E-utilities requests compete for.
    """
    pubmed_options = config.sources.source_options.get("pubmed", {})
    return TransparencyAnalyzer(
        email=config.user.email or DEFAULT_CONTACT_EMAIL,
        pubmed_api_key=pubmed_options.get("api_key") or None,
        settings=build_settings(config),
    )


def run_transparency(
    config: AppConfig,
    *,
    refresh: bool = False,
    paper_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> TransparencyReport:
    """Analyse the transparency of scored papers that still want a result.

    Args:
        config: Application config.
        refresh: Re-analyse papers that already hold a determinate result, and
            restart their retry budget.
        paper_id: Restrict to one publication, ignoring the score gate.
        limit: Cap the batch below :data:`TRANSPARENCY_BATCH_SIZE`.
        dry_run: Report the selection and stop — no analyzer is built, no
            request is made, no row is written.
        on_progress: Optional callback receiving a status message string.

    Returns:
        A :class:`TransparencyReport` for the run.
    """
    if not config.transparency.enabled:
        logger.info("Transparency analysis is disabled — skipping")
        return TransparencyReport()

    batch = limit if limit is not None else TRANSPARENCY_BATCH_SIZE

    with closing(open_db(config)) as conn:
        init_db(conn)

        candidates = get_transparency_candidates(
            conn,
            min_combined=config.transparency.min_combined_score,
            limit=batch,
            max_attempts=TRANSPARENCY_MAX_ATTEMPTS,
            refresh=refresh,
            paper_id=paper_id,
        )
        if not candidates:
            logger.info("No papers awaiting transparency analysis")
            return TransparencyReport()

        total = len(candidates)
        # A full batch cannot be told from an exactly-full queue, so say so
        # rather than leaving the run looking complete — as run_score does.
        if paper_id is None and total == batch:
            logger.warning(
                "Analysing the %d highest-scoring papers awaiting transparency; more "
                "may remain. Re-run `bmnews transparency` to continue.",
                batch,
            )

        if dry_run:
            return TransparencyReport(candidates=total)

        if on_progress:
            on_progress(f"Analysing transparency for {total} paper(s)...")

        return _analyze_all(
            conn,
            _build_analyzer(config),
            candidates,
            refresh=refresh,
            concurrency=config.transparency.concurrency,
            on_progress=on_progress,
        )


def _analyze_all(
    conn: Any,
    analyzer: TransparencyAnalyzer,
    candidates: list[dict],
    *,
    refresh: bool,
    concurrency: int,
    on_progress: Callable[[str], None] | None,
) -> TransparencyReport:
    """Analyse every candidate, storing each result as it lands.

    **One analyzer is shared across the pool** on purpose: bmlib's rate-limit
    lock is per-instance and spans every thread using it, so a second analyzer
    would double the request rate against APIs that asked us not to. Its
    reachability flag is thread-local for the matching reason, so concurrent
    analyses cannot contaminate each other's UNKNOWN.

    **Storing happens here, on the calling thread**, never inside a worker: a
    SQLite connection is not safe to touch from another thread. This mirrors
    ``score_papers``, whose progress callback carries the same guarantee.

    A paper whose analysis raises is logged and skipped, leaving no row — so it
    returns to the queue next run, exactly as an unscoreable paper does.

    Args:
        conn: DB-API connection, used only from this thread.
        analyzer: The shared analyzer.
        candidates: Rows from :func:`get_transparency_candidates`.
        refresh: Whether this run resets each paper's retry budget.
        concurrency: Worker count. Throughput is capped by bmlib's shared
            request interval regardless, so this hides latency rather than
            multiplying speed.
        on_progress: Optional callback receiving a status message string.

    Returns:
        A :class:`TransparencyReport` for the batch.
    """
    total = len(candidates)
    analyzed = indeterminate = exhausted = failed = done = 0

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(
                analyzer.analyze,
                str(paper["id"]),
                pmid=paper.get("pmid") or None,
                doi=paper.get("doi") or None,
            ): paper
            for paper in candidates
        }
        for future in as_completed(futures):
            paper = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception:
                logger.exception(
                    "Transparency analysis failed for paper %s — it stays queued",
                    paper["id"],
                )
                failed += 1
                continue

            risk = result.risk_level.value
            # Derived rather than read back: this is exactly what
            # save_transparency is about to write, and a second query per
            # paper to learn it would be pure overhead.
            attempts = 1 if refresh else (paper.get("attempts") or 0) + 1

            save_transparency(
                conn,
                paper_id=paper["id"],
                transparency_score=result.transparency_score,
                risk_level=risk,
                result_json=json.dumps(result.to_dict()),
                reset_attempts=refresh,
            )
            analyzed += 1
            if risk == _UNKNOWN:
                indeterminate += 1
                if attempts >= TRANSPARENCY_MAX_ATTEMPTS:
                    exhausted += 1

            if on_progress:
                on_progress(f"Analysing transparency {done}/{total}...")

    logger.info(
        "Transparency: %d analysed, %d indeterminate (%d at the attempt ceiling), %d failed",
        analyzed,
        indeterminate,
        exhausted,
        failed,
    )
    return TransparencyReport(
        candidates=total,
        analyzed=analyzed,
        indeterminate=indeterminate,
        exhausted=exhausted,
        failed=failed,
    )


def list_results(config: AppConfig, *, limit: int | None = None) -> list[dict]:
    """Read stored transparency results for reporting, worst risk first.

    Args:
        config: Application config.
        limit: Maximum rows to return.

    Returns:
        Rows as :func:`~bmnews.db.operations.get_transparency_results` returns
        them.
    """
    with closing(open_db(config)) as conn:
        init_db(conn)
        return get_transparency_results(
            conn, limit=limit if limit is not None else TRANSPARENCY_BATCH_SIZE
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transparency.py -v`
Expected: PASS.

If the unused `execute`/`placeholder` imports in the test file trip ruff, delete them — they are only there if a test needs raw SQL.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/transparency/ tests/test_transparency.py
git commit -m "feat(transparency): add the analysis stage

Query-based like the notify stage, so it survives a crash mid-run and tests
without the scorer. One analyzer is shared across the pool because bmlib's
rate-limit lock is per-instance and spans every thread using it — a second
analyzer would double the request rate against APIs that asked us not to —
and its reachability flag is thread-local so concurrent analyses cannot
contaminate each other's UNKNOWN.

Results are stored on the calling thread, never in a worker, because a
SQLite connection is not safe to share. A raising analysis costs only itself
and leaves no row, so it returns to the queue.

The report distinguishes four outcomes a single count would conflate; the
one worth surfacing is 'exhausted', the only outcome waiting cannot fix."
```

---

### Task 5: Wire the stage into the pipeline

**Files:**
- Modify: `bmnews/pipeline.py:453-516` (`run_pipeline` and a new `_run_transparency_stage`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `run_transparency` (Task 4).
- Produces: `pipeline._run_transparency_stage(config, *, on_progress) -> None`, called from `run_pipeline` between `run_score` and `_run_notify_stage`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline.py`, following the mocking style already used there for the notify stage:

```python
class TestTransparencyStage:
    def _config(self, enabled=True):
        config = AppConfig()
        config.transparency.enabled = enabled
        return config

    def test_runs_between_scoring_and_notification(self, monkeypatch):
        """Order matters twice: the gate reads combined_score, and both the
        digest and the notify templates render the badge."""
        calls = []
        monkeypatch.setattr(pipeline, "run_sync", lambda *a, **k: calls.append("sync"))
        monkeypatch.setattr(
            pipeline, "run_score", lambda *a, **k: (calls.append("score"), 1)[1]
        )
        monkeypatch.setattr(
            pipeline,
            "_run_transparency_stage",
            lambda *a, **k: calls.append("transparency"),
        )
        monkeypatch.setattr(pipeline, "_run_notify_stage", lambda *a, **k: calls.append("notify"))
        monkeypatch.setattr(pipeline, "run_digest", lambda *a, **k: calls.append("digest"))

        pipeline.run_pipeline(self._config())

        assert calls == ["sync", "score", "transparency", "notify", "digest"]

    def test_not_gated_on_newly_scored_papers(self, monkeypatch):
        """Loosening min_combined_score must pick up papers scored earlier,
        and a bounded retry is still owed its next attempt."""
        called = []
        monkeypatch.setattr(pipeline, "run_sync", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "run_score", lambda *a, **k: 0)
        monkeypatch.setattr(
            pipeline, "_run_transparency_stage", lambda *a, **k: called.append(True)
        )
        monkeypatch.setattr(pipeline, "_run_notify_stage", lambda *a, **k: None)
        monkeypatch.setattr(
            pipeline, "run_digest", lambda *a, **k: pytest.fail("digest is gated")
        )

        pipeline.run_pipeline(self._config())

        assert called == [True]

    def test_a_failure_does_not_take_the_run_down(self, monkeypatch):
        """Sync and scoring have already paid; an unreachable CrossRef must not
        cost the digest."""
        reached = []

        def _explode(*args, **kwargs):
            raise RuntimeError("CrossRef is down")

        monkeypatch.setattr("bmnews.transparency.service.run_transparency", _explode)
        monkeypatch.setattr(pipeline, "run_sync", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "run_score", lambda *a, **k: 1)
        monkeypatch.setattr(pipeline, "_run_notify_stage", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "run_digest", lambda *a, **k: reached.append("digest"))

        pipeline.run_pipeline(self._config())

        assert reached == ["digest"]

    def test_disabled_config_skips_the_stage_entirely(self, monkeypatch):
        """Assert on a recording, NOT on a raising stub.

        The stage wraps its call in `except Exception` by design, so it swallows
        an AssertionError from a stub exactly as it swallows an httpx error — a
        raising-stub test therefore passes whether or not the enabled gate
        exists at all. Verified: deleting the gate leaves such a test green.
        """
        called = []
        monkeypatch.setattr(
            "bmnews.transparency.service.run_transparency",
            lambda *a, **k: called.append(True),
        )

        pipeline._run_transparency_stage(self._config(enabled=False), on_progress=None)

        assert called == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -k TransparencyStage -v`
Expected: FAIL — `AttributeError: module 'bmnews.pipeline' has no attribute '_run_transparency_stage'`.

- [ ] **Step 3: Add the stage function**

In `bmnews/pipeline.py`, add after `_run_notify_stage` (end of file):

```python
def _run_transparency_stage(
    config: AppConfig, *, on_progress: Callable[[str], None] | None
) -> None:
    """Run the transparency analysis, and never let it take the run down.

    Deliberately **not** gated on ``scored > 0``, for the reasons the notify
    stage is not: loosening ``min_combined_score`` makes papers scored on an
    earlier run newly eligible, and a bounded retry is still owed its next
    attempt even on a run that scored nothing.

    Failures are contained because sync and scoring have already done the
    expensive work by the time this runs, and this stage depends on five
    external APIs — five more things that can be down, none of which should
    cost the digest that would otherwise have gone out.
    """
    if not config.transparency.enabled:
        return

    # Deferred like the notify stage's import: this pulls in bmlib's analyzer
    # and an HTTP client that `bmnews fetch` should not pay for.
    from bmnews.transparency.service import run_transparency

    try:
        run_transparency(config, on_progress=on_progress)
    except Exception:
        logger.exception("Transparency stage failed — continuing with the pipeline")
```

- [ ] **Step 4: Call it from `run_pipeline`**

In `run_pipeline`, insert between the `run_score` call (line 481) and `_run_notify_stage` (line 483):

```python
    _run_transparency_stage(config, on_progress=on_progress)
```

and update the docstring's first line and the `run_pipeline` summary:

```python
    """Execute the full pipeline: fetch → store → score → transparency → notify → digest.
```

Also update the module docstring at line 3:

```python
Runs the full fetch → store → score → transparency → notify → digest cycle.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): run transparency between scoring and notification

After SCORE because the gate reads combined_score; before NOTIFY and DIGEST
because both render the badge.

Two properties inherited from the notify stage on purpose. It is not gated on
scored > 0 — loosening min_combined_score makes already-scored papers
eligible, and a bounded retry is owed its next attempt on a run that scores
nothing. And it is failure-contained: five external APIs are five more things
that can be down, and by the time this runs sync and scoring have already
paid, so none of them should cost the digest."
```

---

### Task 6: The `bmnews transparency` CLI command

**Files:**
- Modify: `bmnews/cli.py` (add the command after `notify`, before `init` at line 175)
- Test: `tests/test_transparency.py` (append a `TestCli` class)

**Interfaces:**
- Consumes: `run_transparency`, `list_results`, `TransparencyReport` (Task 4).
- Produces: the `bmnews transparency` command.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transparency.py`:

```python
class TestCli:
    """The `bmnews transparency` command."""

    def _invoke(self, args, config):
        from click.testing import CliRunner

        from bmnews.cli import main

        return CliRunner().invoke(main, args, obj={"config": config})

    def test_disabled_config_says_so(self, db):
        config, _ = db
        config.transparency.enabled = False

        result = self._invoke(["transparency"], config)

        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_reports_what_it_analysed(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/a", combined=0.9)
        _install(monkeypatch, _FakeAnalyzer())

        result = self._invoke(["transparency"], config)

        assert result.exit_code == 0
        assert "1" in result.output

    def test_nothing_to_do_is_stated(self, db, monkeypatch):
        config, _ = db
        _install(monkeypatch, _FakeAnalyzer())

        result = self._invoke(["transparency"], config)

        assert "othing" in result.output

    def test_dry_run_reports_the_selection(self, db, monkeypatch):
        config, conn = db
        _scored(conn, doi="10.1/a", combined=0.9)

        def _boom(**kwargs):
            raise AssertionError("dry run must not build an analyzer")

        monkeypatch.setattr(service, "TransparencyAnalyzer", _boom)

        result = self._invoke(["transparency", "--dry-run"], config)

        assert result.exit_code == 0
        assert "1" in result.output

    def test_list_prints_stored_results(self, db):
        config, conn = db
        paper_id = _scored(conn, doi="10.1/a", combined=0.9)
        save_transparency(
            conn, paper_id=paper_id, transparency_score=20, risk_level="high",
            result_json='{"risk_indicators": ["No COI disclosure found in full text"]}',
        )

        result = self._invoke(["transparency", "--list"], config)

        assert "HIGH" in result.output
        assert "No COI disclosure" in result.output

    def test_list_with_nothing_stored(self, db):
        config, _ = db

        result = self._invoke(["transparency", "--list"], config)

        assert "o results" in result.output

    def test_limit_must_be_positive(self, db):
        config, _ = db

        result = self._invoke(["transparency", "--limit", "0"], config)

        assert result.exit_code != 0
        assert "at least 1" in result.output

    def test_list_refuses_analysis_flags(self, db):
        """--list says 'analyse nothing'; --refresh says 'analyse again'.
        Honouring one and dropping the other silently is the failure mode."""
        config, _ = db

        result = self._invoke(["transparency", "--list", "--refresh"], config)

        assert result.exit_code != 0

    def test_paper_id_is_passed_through(self, db, monkeypatch):
        config, conn = db
        low = _scored(conn, doi="10.1/low", combined=0.01)
        analyzer = _FakeAnalyzer()
        _install(monkeypatch, analyzer)

        result = self._invoke(["transparency", "--paper-id", str(low)], config)

        assert result.exit_code == 0
        assert analyzer.calls == [(str(low), None, "10.1/low")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transparency.py -k TestCli -v`
Expected: FAIL — `No such command 'transparency'`.

- [ ] **Step 3: Add the command**

In `bmnews/cli.py`, insert before the `init` command (line 175):

```python
@main.command()
@click.option("--limit", default=None, type=int, help="Analyse at most this many papers.")
@click.option("--refresh", is_flag=True, help="Re-analyse papers that already have a result.")
@click.option("--paper-id", default=None, type=int, help="Restrict to one paper, ignoring the gate.")
@click.option("--list", "list_only", is_flag=True, help="Print stored results; analyse nothing.")
@click.option("--dry-run", is_flag=True, help="Report what would be analysed; call no API.")
@click.pass_context
def transparency(
    ctx: click.Context,
    limit: int | None,
    refresh: bool,
    paper_id: int | None,
    list_only: bool,
    dry_run: bool,
) -> None:
    """Assess research integrity for scored papers.

    Checks funder disclosure, COI statements, data availability and trial
    results reporting against CrossRef, Europe PMC, PubMed, OpenAlex and
    ClinicalTrials.gov. Results are displayed beside a paper and never change
    which papers are selected or how they rank.

    Each analysis costs several external requests, so only papers scoring above
    transparency.min_combined_score are analysed. --paper-id ignores that gate.
    """
    from bmnews.transparency import service

    config = ctx.obj["config"]

    if limit is not None and limit < 1:
        raise click.UsageError("--limit must be at least 1.")
    # Refusing rather than quietly ignoring: --list means "analyse nothing" and
    # these two mean "analyse differently", so honouring one and dropping the
    # other would do something the user did not ask for.
    if list_only and (refresh or dry_run):
        raise click.UsageError("--list analyses nothing; drop --refresh/--dry-run.")

    if list_only:
        rows = service.list_results(config, limit=limit)
        if not rows:
            click.echo("No results stored yet. Run `bmnews transparency` first.")
            return
        for row in rows:
            click.echo(
                f"{row['risk_level'].upper()} {row['transparency_score']}/100 — "
                f"{row['title']} ({row['doi'] or 'no DOI'})"
            )
            for indicator in parse_transparency(row["result_json"]).get("risk_indicators", []):
                click.echo(f"    - {indicator}")
        return

    if not config.transparency.enabled:
        click.echo(
            "Transparency analysis is disabled. Set enabled = true under "
            "[transparency] in your config to turn it on."
        )
        return

    report = service.run_transparency(
        config, refresh=refresh, paper_id=paper_id, limit=limit, dry_run=dry_run
    )

    if dry_run:
        click.echo(f"Would analyse {report.candidates} paper(s).")
        return

    if not report.analyzed and not report.failed:
        click.echo("Nothing to analyse.")
        return

    click.echo(f"Analysed {report.analyzed} paper(s).")
    if report.indeterminate:
        click.echo(
            f"{report.indeterminate} could not be determined "
            f"({report.exhausted} will not be retried without --refresh)."
        )
    if report.failed:
        click.echo(
            f"{report.failed} analysis attempt(s) failed — they stay queued and retry.",
            err=True,
        )
```

Add the import near the top of `bmnews/cli.py`, alongside the other module-level imports:

```python
from bmnews.metadata import parse_transparency
```

> `--list` is checked before the `enabled` guard on purpose: reading results already stored is meaningful whether or not the feature is currently switched on.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_transparency.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/cli.py tests/test_transparency.py
git commit -m "feat(cli): add bmnews transparency

Flags follow bmnews notify's vocabulary. --dry-run earns its place here more
than elsewhere: it reports what a loosened min_combined_score would cost
before a single request is spent.

--list is checked before the enabled guard, because results already stored
are worth reading whether or not the feature is currently on. It refuses
--refresh and --dry-run rather than ignoring them, since it means 'analyse
nothing' and they mean 'analyse differently'."
```

---

### Task 7: The four display surfaces

**Files:**
- Modify: `bmnews/gui/templates/fragments/reading_pane.html:18-32`, `bmnews/gui/static/css/app.css` (after `.tier-badge`, ~line 135)
- Modify: `templates/digest_email.html:34-38`, `templates/digest_text.txt:13`
- Modify: `templates/notify_email.html`, `templates/notify_email.txt`, `templates/notify_matrix.html`, `templates/notify_matrix.txt`
- Test: `tests/test_gui_app.py`, `tests/test_digest.py`, `tests/test_notify_channels.py`

**Interfaces:**
- Consumes: `transparency_risk`, `transparency_score` on every paper dict, and `transparency` on the detail view (Task 3).
- Produces: no Python API. Read `bmnews/gui/CLAUDE.md` before touching anything under `bmnews/gui/`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_digest.py`. That file has no fixtures — it builds a `TemplateEngine` inline in each test and defines a module-level `TEMPLATES_DIR`; match that exactly:

```python
class TestTransparencyBadge:
    def _paper(self, **overrides):
        paper = {
            "title": "Effect of Drug X",
            "url": "https://doi.org/10.1/a",
            "authors": "Smith J",
            "published_date": "2026-07-30",
            "source": "medrxiv",
            "summary": "Summary.",
            "relevance_score": 0.9,
            "quality_tier": "TIER_2_OBSERVATIONAL",
            "study_design": "cohort",
        }
        paper.update(overrides)
        return paper

    def test_html_shows_the_badge(self):
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        papers = [self._paper(transparency_risk="high", transparency_score=25)]

        html = render_digest(papers, engine, fmt="html")

        assert "HIGH" in html

    def test_text_shows_the_badge(self):
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        papers = [self._paper(transparency_risk="low", transparency_score=85)]

        text = render_digest(papers, engine, fmt="text")

        assert "LOW" in text

    def test_unanalysed_paper_renders_no_badge(self):
        """An empty risk reads as 'not analysed', exactly as quality_tier does."""
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)
        papers = [self._paper(transparency_risk="")]

        html = render_digest(papers, engine, fmt="html")

        assert "Transparency" not in html

    def test_papers_predating_the_column_still_render(self):
        """Every existing caller passes dicts without the key at all."""
        engine = TemplateEngine(default_dir=TEMPLATES_DIR)

        html = render_digest([self._paper()], engine, fmt="html")

        assert "Effect of Drug X" in html
```

Add to `tests/test_gui_app.py`. Note the fixture shape there: `app` provides the connection as `app.config["BMNEWS_DB"]` and `client` is derived from `app`, so a test needing both takes `app` and builds the client itself. Add `save_transparency` to the file's imports, and `json` if absent:

```python
def test_reading_pane_shows_the_transparency_badge(app):
    conn = app.config["BMNEWS_DB"]
    paper_id = store_paper(
        conn, doi="10.1/a", title="A paper", abstract="Abstract", source="medrxiv"
    )
    save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)
    save_transparency(
        conn,
        paper_id=paper_id,
        transparency_score=25,
        risk_level="high",
        result_json=json.dumps(
            {
                "industry_funding_detected": True,
                "data_availability_level": "not_stated",
                "coi_disclosed": False,
                "trial_registered": False,
                "trial_results_compliant": False,
                "risk_indicators": ["No COI disclosure found in full text"],
            }
        ),
    )

    response = app.test_client().get(f"/papers/{paper_id}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "risk-high" in body
    assert "No COI disclosure found in full text" in body
    assert "industry ties detected" in body


def test_reading_pane_omits_transparency_when_unanalysed(app):
    conn = app.config["BMNEWS_DB"]
    paper_id = store_paper(
        conn, doi="10.1/b", title="B paper", abstract="Abstract", source="medrxiv"
    )
    save_score(conn, paper_id=paper_id, relevance_score=0.9, combined_score=0.9)

    body = app.test_client().get(f"/papers/{paper_id}").get_data(as_text=True)

    assert "risk-badge" not in body
    assert "Research integrity" not in body
```

Add to `tests/test_notify_channels.py`. `render_notification` takes `papers` positionally and **everything else keyword-only**, including `templates` — mirror the `_render` helper already in that file:

```python
class TestTransparencyBadge:
    def _papers(self):
        return [
            {
                "title": "Adjuvant immunotherapy in melanoma",
                "url": "https://doi.org/10.1101/one",
                "authors": ["Smith J"],
                "sources": ["medrxiv"],
                "publication_date": "2026-07-30",
                "relevance_score": 0.9,
                "quality_tier": "TIER_2_OBSERVATIONAL",
                "study_design": "cohort",
                "summary": "Summary.",
                "transparency_risk": "medium",
                "transparency_score": 60,
            }
        ]

    def _render(self, medium, fmt, papers=None):
        from pathlib import Path

        from bmlib.templates import TemplateEngine

        from bmnews.notify.renderer import render_notification

        engine = TemplateEngine(default_dir=Path(__file__).parent.parent / "templates")
        return render_notification(
            papers if papers is not None else self._papers(),
            watch_name="melanoma-trials",
            templates=engine,
            medium=medium,
            fmt=fmt,
        )

    @pytest.mark.parametrize("medium", ["email", "matrix"])
    @pytest.mark.parametrize("fmt", ["html", "text"])
    def test_every_template_shows_the_badge(self, medium, fmt):
        assert "MEDIUM" in self._render(medium, fmt)

    @pytest.mark.parametrize("medium", ["email", "matrix"])
    @pytest.mark.parametrize("fmt", ["html", "text"])
    def test_unanalysed_paper_renders_no_badge(self, medium, fmt):
        papers = self._papers()
        papers[0]["transparency_risk"] = ""

        assert "transparency" not in self._render(medium, fmt).lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_digest.py tests/test_gui_app.py tests/test_notify_channels.py -k "Transparency or transparency" -v`
Expected: FAIL — the badge text is absent. (`-k` is a case-sensitive substring match, so both spellings are needed to catch the `TestTransparencyBadge` classes *and* the two lowercase GUI test names.)

- [ ] **Step 3: Add the CSS**

In `bmnews/gui/static/css/app.css`, after `.tier-badge` (line 135):

```css
.risk-badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 10px;
    font-size: 0.75rem;
    font-weight: 600;
}
.risk-low { background: #d4edda; color: #155724; }
.risk-medium { background: #fff3cd; color: #856404; }
.risk-high { background: #f8d7da; color: #721c24; }
.risk-unknown { background: #eceff1; color: #546e7a; }

.transparency-findings, .risk-indicators { margin: 0.5rem 0 0 1.25rem; font-size: 0.85rem; }
.risk-indicators { color: var(--text-muted); }
```

- [ ] **Step 4: Add the badge and findings to the reading pane**

In `bmnews/gui/templates/fragments/reading_pane.html`, inside the first `score-section` block, after the `design-badge` conditional (line 27):

```html
        {% if paper.transparency_risk %}
        <span class="risk-badge risk-{{ paper.transparency_risk }}">
            Transparency: {{ paper.transparency_risk|upper }}{% if paper.transparency_risk != 'unknown' %} {{ paper.transparency_score }}/100{% endif %}
        </span>
        {% endif %}
```

Then add a findings section immediately after the summary section (after line 39):

```html
    {% if paper.transparency %}
    <section class="transparency-section">
        <h2>Research integrity</h2>
        <ul class="transparency-findings">
            <li>Funding:
                {% if paper.transparency.industry_funding_detected %}industry ties detected
                {% else %}no industry ties detected{% endif %}</li>
            <li>Data availability: {{ paper.transparency.data_availability_level|default('unknown')|replace("_", " ") }}</li>
            <li>COI statement:
                {% if paper.transparency.coi_disclosed is none %}undetermined
                {% elif paper.transparency.coi_disclosed %}disclosed
                {% else %}absent{% endif %}</li>
            <li>Trial registration:
                {% if paper.transparency.trial_registered %}registered{% if paper.transparency.trial_results_compliant %}, results posted{% else %}, results not posted{% endif %}
                {% else %}none found{% endif %}</li>
        </ul>
        {% if paper.transparency.risk_indicators %}
        <ul class="risk-indicators">
            {% for indicator in paper.transparency.risk_indicators %}<li>{{ indicator }}</li>{% endfor %}
        </ul>
        {% endif %}
    </section>
    {% endif %}
```

> No `|e` here: Flask's Jinja environment autoescapes, so the API-derived funder names inside `risk_indicators` are already safe. The templates below are a different environment — see the next step.

- [ ] **Step 5: Add the badge to the digest templates**

In `templates/digest_email.html`, inside the `.scores` div (after line 37):

```html
        {% if paper.transparency_risk %}<span class="score-badge">Transparency: {{ paper.transparency_risk|upper }}</span>{% endif %}
```

In `templates/digest_text.txt`, append to the scores line (line 13, before the newline):

```
{% if paper.transparency_risk %} | Transparency: {{ paper.transparency_risk|upper }}{% endif %}
```

- [ ] **Step 6: Add the escaped badge to the four notify templates**

bmlib's `TemplateEngine` runs `autoescape=False`, so these interpolate explicitly, as every other value in these files already does. Only `risk_level` is rendered — never `risk_indicators`, which is what keeps API-derived prose away from this engine entirely.

`templates/notify_email.html`, in the `.scores` div:

```html
        {% if paper.transparency_risk %}<span class="score-badge">Transparency {{ paper.transparency_risk|upper|e }}</span>{% endif %}
```

`templates/notify_matrix.html`, appended inside the `<em>` metadata line, before the closing `</em>`:

```html
{% if paper.transparency_risk %} &middot; transparency {{ paper.transparency_risk|upper|e }}{% endif %}
```

`templates/notify_email.txt` and `templates/notify_matrix.txt`, appended to each paper's metadata line:

```
{% if paper.transparency_risk %} | transparency {{ paper.transparency_risk|upper }}{% endif %}
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_digest.py tests/test_gui_app.py tests/test_notify_channels.py -v`
Expected: PASS.

- [ ] **Step 8: See it in the real app**

Run: `uv run bmnews gui`
Open a paper with a stored result and confirm the badge and the findings list render, in both the light and the default theme the CSS variables provide.

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check bmnews/ tests/ && uv run ruff format bmnews/ tests/
git add bmnews/gui/ templates/ tests/test_digest.py tests/test_gui_app.py tests/test_notify_channels.py
git commit -m "feat(ui): show the transparency badge on all four surfaces

The reading pane gets the badge plus the findings and the indicator list;
Flask's Jinja autoescapes, so the API-derived funder names in those
indicators are safe there.

The digest and notify templates get the badge only — risk_level and nothing
else. That is deliberate: bmlib's TemplateEngine runs autoescape=False, and
rendering only an enum value and an integer leaves those surfaces with no
injection exposure at all rather than an escaped one. The interpolations are
still escaped where their neighbours are."
```

---

### Task 8: Documentation, the digest-escaping issue, and full verification

**Files:**
- Modify: `docs/user/configuration.md:190-205` and `:400`, `docs/user/usage.md`, `docs/user/installation.md:36-38`
- Modify: `docs/dev/bmlib-integration.md:216-220`, `docs/dev/database.md`, `docs/dev/architecture.md`, `docs/dev/codebase.md`
- Modify: `CLAUDE.md`, `HANDOVER.md`
- Modify: `docs/plans/2026-07-30-transparency-analysis-design.md` (status line)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Update the user documentation**

`docs/user/configuration.md` — rewrite the `[transparency]` section: rename `min_score_threshold` to `min_combined_score` and note the old name is carried forward with a warning; add `score_threshold` and `concurrency`; state plainly that the analysis is display-only and never changes selection or ranking; correct the description from "publication bias and integrity" to what bmlib actually assesses. Update the full example config at line 400 to match.

`docs/user/usage.md` — document `bmnews transparency` with each flag, and add TRANSPARENCY to the pipeline stage list.

`docs/user/installation.md` — drop "declared but not yet wired up" from the `transparency` extra, and note that it resolves to `httpx`, which bmnews already requires, so the extra installs nothing new.

- [ ] **Step 2: Update the developer documentation**

`docs/dev/bmlib-integration.md:216-220` — replace the "not wired up" section with how the analyzer is used: `run_transparency`, the settings mapping (including that the two `*_triggers_downgrade` flags shape the displayed risk level), and that `tier_downgrade_applied` is stored but not applied.

`docs/dev/database.md` — add the `transparency` table to the bmnews-owned list with the `attempts` rationale, and migration 7 to the migration list.

`docs/dev/architecture.md` — add TRANSPARENCY to the data-flow diagram and `bmnews/transparency/` to the module graph.

`docs/dev/codebase.md` — add the `transparency/` package and the two new constants.

- [ ] **Step 3: Update CLAUDE.md**

Four edits:
- Remove `bmlib.transparency` from **"Not currently used"** and add a `bmlib.transparency` row to the bmlib integration table.
- Change the pipeline line to `SYNC → SCORE → TRANSPARENCY → NOTIFY → DIGEST`, and note the stage is ungated on `scored > 0` and failure-contained.
- Add `transparency` to the bmnews-owned tables, and migration 7 to the migration list.
- Add `tests/test_transparency.py` to the test-file table; extend the `test_db.py` row with the new table and migration.

- [ ] **Step 4: File the pre-existing digest-escaping issue**

Not caused by this work, but found by it: `templates/digest_email.html` interpolates `paper.title`, `paper.summary` and the author list **unescaped**, while the four `notify_*` templates escape theirs and say why. Both render through bmlib's `TemplateEngine` with `autoescape=False`. Do not fix it here — it is unrelated to transparency and deserves its own change.

```bash
gh issue create --title "digest templates do not escape third-party metadata" --body "$(cat <<'EOF'
`templates/digest_email.html` and `templates/digest_text.txt` interpolate
`paper.title`, `paper.summary` and the author list with no `|e`, while the four
`notify_*` templates escape every interpolation and carry a comment explaining
why. Both render through bmlib's `TemplateEngine`, which runs with
`autoescape=False` because its first job was plain-text LLM prompts.

So a preprint title containing markup reaches the reader's mail client as
markup. Mail clients strip scripting, so this is a rendering-integrity problem
rather than a scripting one — but it is the same defect the notify templates
were deliberately written to avoid, and the project's stated rule is that these
templates escape their own interpolations.

Found while adding the transparency badge (PR for
docs/plans/2026-07-30-transparency-analysis-design.md), which only renders an
enum value and an integer and so was not affected.

Fix: add `|e` to every third-party interpolation in the two digest templates,
and a comment matching the notify templates' explanation.
EOF
)"
```

- [ ] **Step 5: Update the design document's status**

Change its status line to record that it shipped:

```markdown
**Status:** implemented — see `docs/plans/2026-07-30-transparency-analysis-plan.md`.
```

- [ ] **Step 6: Update HANDOVER.md**

Move `bmlib.transparency` out of the open-items table and into a short "what shipped" section covering: the stage's placement and why it is ungated and contained; the `attempts` ceiling and the `UNREACHABLE` ambiguity that forces it (this is the single most important thing not to undo); the config rename; that it informs only, with the tier downgrade and digest filtering deliberately deferred; and that the bmlib pin still needs bumping to 0.6.0. Keep the file under 500 lines by pruning the older `publications`-migration reference material as needed.

Also update the "Environment gotcha" section: the pin is still at 0.5.1 and this feature deliberately does not need moving it.

- [ ] **Step 7: Full verification**

Every command must pass. Do not proceed on a failure — fix it.

```bash
uv run pytest tests/ -v
BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5432/bmnews_test uv run pytest tests/test_db.py -v
BMNEWS_TEST_PG_DSN=postgresql://hherb@localhost:5532/bmnews_test uv run pytest tests/test_db.py -v
uv run ruff check bmnews/ tests/
uv run ruff format --check bmnews/ tests/
```

Confirm the PostgreSQL run reports **no skips** for `tests/test_db.py`. Then check the CLI is wired:

```bash
uv run bmnews transparency --help
uv run bmnews --help    # transparency should be listed
```

- [ ] **Step 8: Commit and open the PR**

```bash
git add docs/ CLAUDE.md HANDOVER.md
git commit -m "docs: record the transparency stage

Removes bmlib.transparency from 'not currently used' in CLAUDE.md and the
dev manual, adds the TRANSPARENCY stage to both pipeline diagrams, documents
the new table and migration 7, and rewrites the [transparency] config
reference around the renamed gate and what the analysis actually assesses.

HANDOVER.md moves transparency out of the open items and records the one
invariant not to undo: the attempts ceiling exists because bmlib reports
UNREACHABLE both for an outage and for a paper indexed nowhere."

git push -u origin feat/transparency-analysis
gh pr create --base main --title "feat: wire up bmlib.transparency as an inform-only pipeline stage" --body "$(cat <<'EOF'
## What

`[transparency]` has been in `config.toml` and `pyproject.toml` since the first
release while nothing ever called an analyzer — `enabled = true` was a silent
no-op. This wires it up.

A fifth pipeline stage, `SYNC → SCORE → TRANSPARENCY → NOTIFY → DIGEST`,
analyses scored papers above a combined-score gate through
`bmlib.transparency` and stores the result in a new `transparency` table.
Four surfaces display it: the GUI reading pane (badge + findings), the email
digest, watch notifications, and `bmnews transparency --list`.

Design: `docs/plans/2026-07-30-transparency-analysis-design.md`.
Plan: `docs/plans/2026-07-30-transparency-analysis-plan.md`.

## Decisions reviewers should check

**It informs only.** No selection query filters on transparency, the matcher is
untouched, and `tier_downgrade_applied` is stored but never applied. A value
derived from five external APIs must not be able to move a `combined_score` the
user has already acted on. Filtering and the downgrade are both additive later.

**Retries are bounded by an attempt count, not by `unknown_reason`.** This is
the least obvious decision in the PR. bmlib sets its reachability flag only on
an HTTP 200, so it reports `UNREACHABLE` both for a network outage *and* for a
paper indexed in none of the five APIs — the two are indistinguishable.
"Retry every `UNREACHABLE`" therefore means re-querying every unindexed
preprint on every run, four to eight requests each, forever. The
`transparency.attempts` column stops that at 3; `--refresh` resets it, because
an explicit re-analysis has to restore the automatic retries too or a single
attempt would exhaust the budget.

**`min_score_threshold` → `min_combined_score`.** It sat one field away from
bmlib's `score_threshold` meaning something different on a different scale.
The value is carried forward with a warning, because `_apply_section` assigns
only to attributes that exist and would otherwise have reverted a customised
gate to its default silently. Safe to rename because the feature never ran.

**The digest and notify templates render the badge only**, never
`risk_indicators`. Those templates go through bmlib's `TemplateEngine` with
`autoescape=False`, so keeping API-derived prose out of them entirely leaves no
injection exposure rather than an escaped one. The reading pane renders the
full findings because Flask's Jinja autoescapes.

**The bmlib pin is untouched.** Every symbol used exists in the pinned 0.5.1;
the design deliberately avoids `TransparencyUnknownReason`, which the pin lacks.
Bumping to 0.6.0 is a lock-file change afterwards and needs no code to move
with it — `unknown_reason` then starts appearing inside `result_json` on its
own, since the whole `to_dict()` blob is stored.

## Testing

- `tests/test_transparency.py` — the stage and the CLI, analyzer mocked
  throughout. Covers the gate, the attempt ceiling, `--refresh` resetting the
  budget, `--paper-id` bypassing the gate, `--dry-run` building no analyzer, a
  raising analysis costing only itself, and concurrency storing every result.
- `tests/test_db.py` — the table, migration 7 and the read path, **run against
  SQLite and PostgreSQL**, since both `db/operations.py` and
  `db/migrations.py` changed.
- Badge coverage in `test_digest.py`, `test_gui_app.py`,
  `test_notify_channels.py`; stage placement and containment in
  `test_pipeline.py`; the rename in `test_config.py`.

Full suite and both lint checks pass.

## Follow-ups

- Bump bmlib to 0.6.0 and `uv lock --upgrade-package bmlib`. No code change.
- Issue filed for the pre-existing lack of escaping in the digest templates,
  found while adding the badge but unrelated to it.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage.** Every section of the design maps to a task: config and the rename → 1; storage, migration 7 and the three `attempts` rules → 2; the read path, `_NOTIFY_PAPER_COLUMNS` and the plain-dict decoder → 3; the stage, the shared analyzer, calling-thread storage, `TransparencyReport` and the `TransparencySettings` mapping → 4; pipeline placement, ungated and contained → 5; all six CLI flags → 6; all four surfaces → 7; every listed doc plus the out-of-scope list → 8. The design's "deliberately out of scope" items appear only as Global Constraints forbidding them.

**Placeholders.** None. Every code step carries the actual code; every test step carries the actual assertions; the two `--list`/badge orderings and the escaping split are spelled out rather than described.

**Type consistency.** `save_transparency`'s `reset_attempts` is the same name in Task 2's definition, Task 4's call and Task 2's tests. `get_transparency_candidates` returns `attempts` (Task 2) which Task 4 reads as `paper.get("attempts") or 0`. `transparency_risk` / `transparency_score` / `transparency` are spelled identically in Tasks 3, 6 and 7. `TransparencyReport`'s five fields match between the dataclass, the service's return, and the CLI's reads. `parse_transparency` is defined in Task 3 and imported by both `operations.py` (Task 3) and `cli.py` (Task 6).

**Existing signatures were checked against the code, not assumed.** Three were wrong on the first pass and are corrected above:

- `render_notification(papers, *, watch_name, templates, medium, fmt, remaining=0)` — everything after `papers` is keyword-only, and the engine parameter is `templates`, not a positional `template_engine`.
- `tests/test_gui_app.py` has no `conn` fixture. The connection is `app.config["BMNEWS_DB"]`, and `client` is derived from `app`, so a test needing both takes `app`.
- `tests/test_digest.py` has no fixtures at all; it constructs `TemplateEngine(default_dir=TEMPLATES_DIR)` inline per test.

`store_paper` is keyword-only with `title` required and `doi`/`pmid` optional, which is what every test above relies on. `render_digest(papers, engine, *, subject_prefix=..., fmt=...)` takes the engine positionally, unlike `render_notification` — the asymmetry is pre-existing and is why both were checked.

**One thing an implementer must not "tidy".** `get_transparency_candidates` returns `attempts` purely so `_analyze_all` can derive the value `save_transparency` is about to write, instead of reading the row back per paper. Dropping the column looks like cleanup and silently adds a query per analysed paper.
