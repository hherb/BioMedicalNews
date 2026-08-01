# Design: a CI backstop for docs/dev drift

**Issue:** [#16](https://github.com/hherb/BioMedicalNews/issues/16)
**Date:** 2026-08-01
**Status:** approved

## Problem

Nothing fails when `docs/dev/` goes stale. The rewrite in PR #15 was verified
by reading the code and cross-checking every symbol by hand — that
verification is not repeatable and does not run in CI. The next rename
re-opens exactly the drift issue #11 was filed for, and the only signal is a
reader noticing.

## Scope

Exact-match checks only (the user's explicit decision on 2026-08-01). The
symbol-resolution check from the issue — backticked `some_function()`
references resolving to a `def` in `bmnews/` — needs heuristics and a larger
allowlist, and is deferred: the PR closes #16 as the first pass the issue
itself proposed, and the symbol check gets a fresh, narrower issue if it is
still wanted once this backstop has run for a while.

Only `docs/dev/*.md` is checked. `CLAUDE.md`, `HANDOVER.md` and `docs/user/`
are out of scope, deliberately: the issue is about the developer manual, and
widening the net means widening the allowlist.

## Decision

One new test module, `tests/test_docs.py`, in the ordinary pytest suite. CI
already runs pytest, so the backstop costs no new CI wiring, and developers
hit it locally with the suite they already run. It reads files and imports
`bmnews.db.migrations` — no database, no LLM, no network, no fixtures beyond
the repo tree itself.

Alternatives rejected: a standalone `scripts/check_docs.py` with its own CI
step (more moving parts, nobody runs it locally) and a docs-tooling
dependency such as a link checker (these docs are not built, and no
off-the-shelf tool knows about the migration or test-file tables anyway).

## The three checks

### 1. Backticked paths exist

Scan inline backticked tokens in every `docs/dev/*.md`. Fenced code blocks
are excluded — they hold example code, not references.

A token is a **path candidate** when all of these hold:

- it matches `[A-Za-z0-9_./-]+` in full (this drops URLs, which contain `:`)
- it contains `/`
- it ends with `/` or with a known extension:
  `.py .md .toml .txt .html .css .js .json`

(`n-1/n` in codebase.md fails the ending test; prose fragments and option
strings fail the character test.)

Candidates are then filtered:

- tokens starting with `bmlib/` are skipped — that is a different repository,
  and checking them against the *installed* bmlib would couple the docs check
  to whatever version this machine resolves
- tokens starting with `~` are skipped — user-home paths
  (`~/.bmnews/config.toml`) name runtime files, not repo files
- tokens in `KNOWN_FICTIONAL_PATHS`, a module-level frozenset with a comment,
  are skipped. It currently holds only `bmnews/fetchers/newsource.py`, the
  worked example in `contributing.md` and `bmlib-integration.md` that a
  reader would create. Anything added to it needs the same justification.

A surviving candidate **passes** if it exists relative to any base in a
deterministic set: the repo root, `bmnews/`, or any direct subdirectory of
`bmnews/` that is a package (computed from the tree at test time, not
hardcoded). The bases resolve the docs' package-relative shorthand —
`db/operations.py` via `bmnews/`, `channels/` via `bmnews/notify/` — without
per-token rules. A stale path that happens to resolve against the wrong base
is a false negative this design accepts: the check is a backstop, not a
proof.

The failure message lists **every** failing token with its doc file and line
number, so one run shows the whole cleanup.

### 2. The migration table matches `MIGRATIONS`

`docs/dev/database.md` documents the migrations in a table whose header row
is `| # | Name | What it does |`. The check parses that table — anchored on
the header — and compares the set of `(version, name)` pairs against
`[(m.version, m.name) for m in MIGRATIONS]` from `bmnews.db.migrations`,
in both directions: a migration added to code but missing from the doc fails
exactly as a documented migration missing from code does. The prose column is
not compared — what a migration does is for humans to describe.

If no table with that header exists, the check fails with "migration table
not found in database.md" rather than passing vacuously: renaming the header
is itself drift.

### 3. The test-file listing matches `tests/`

`docs/dev/testing.md` lists the suite in a fenced code block whose first line
is `tests/`. The check extracts every `*.py` filename from that block and
compares the set against the actual `tests/*.py` files (excluding
`__init__.py`), in both directions — a new test file must be documented, a
documented file must exist. Comments after the filenames are ignored.

As with the migration table, a missing block is a failure, not a pass.

## Testing the checker

The parsers (inline-backtick extraction with fence exclusion, the two table
parsers, the path-candidate filter) are module-level functions tested against
literal fixture strings — including a fenced block containing backticks, the
`n-1/n` non-path, a URL, and a `bmlib/` token. Each of the three checks is
demonstrated to fail on seeded drift (a fictional path, a migration pair
removed, a test file removed from the listing) via the fixture-string tests;
the three real checks then run against the live tree and must pass.

## Consequences

- Adding a migration now requires touching `database.md`; adding a test file
  requires touching `testing.md`. That is the point.
- A doc author inventing a new worked-example path must either name it under
  a real directory that exists or add it to `KNOWN_FICTIONAL_PATHS` — the
  failure message says so.
- The check never imports GUI or pipeline code, so it stays fast and cannot
  flake on external state.
