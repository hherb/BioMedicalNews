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

Only `docs/dev/*.md` is checked for paths. `HANDOVER.md` and `docs/user/` are
out of scope, deliberately: the issue is about the developer manual, and
widening the *path* net means widening the allowlist.

`CLAUDE.md` is a partial exception, added in the review revision below: its
test-file table and module count are checked against `tests/`, because that
comparison needs no allowlist at all and the count was demonstrably the drift
that happened (it said 14 while the suite held 17). Its backticked paths are
still unchecked.

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

### 3. The test-file listings match `tests/`

`docs/dev/testing.md` lists the suite in a fenced code block whose first line
is `tests/`. The check extracts every `*.py` filename from that block and
compares the set against the actual `tests/*.py` files (excluding
`__init__.py`), in both directions — a new test file must be documented, a
documented file must exist. Comments after the filenames are ignored.

`CLAUDE.md` carries the same listing twice more — a `| File | Coverage |`
table and a `# Test suite (N test modules)` comment in its directory tree —
and both are compared against `tests/` the same way. The table parser reads
only each row's **first** cell, so a filename mentioned in a coverage
description cannot pass a file off as documented; one cell may name several
files (`backends.py` / `conftest.py`). The count is of `test_*.py` modules,
which is what that number has always meant; the wording was made explicit so
the check has an unambiguous anchor.

As with the migration table, a missing block, table or comment is a failure,
not a pass.

## Testing the checker

The parsers (inline-backtick extraction with fence exclusion, the three table
and listing parsers, the path-candidate filter, the fence-balance check) are
module-level functions tested against literal fixture strings — including a
fenced block containing backticks, the `n-1/n` non-path, a URL, and a
`bmlib/` token.

The path check's own logic — the skip prefixes, `KNOWN_FICTIONAL_PATHS`, base
resolution, failure formatting — is a module-level function too
(`unresolved_paths`), tested against seeded drift in `tmp_path`: a missing
path reported with file and line, a fenced example not reported, the skipped
prefixes and the allowlist honoured (and the same doc failing with an empty
allowlist), and an unclosed fence reported. Leaving that logic inside the test
body would have let a broadened skip retire the check with nothing noticing.

The five real checks then run against the live tree and must pass.

## Consequences

- Adding a migration now requires touching `database.md`; adding a test file
  requires touching `testing.md` *and* `CLAUDE.md`'s table (and its count, for
  a `test_*.py`). That is the point.
- A doc author inventing a new worked-example path must either name it under
  a real directory that exists or add it to `KNOWN_FICTIONAL_PATHS` — the
  failure message says so.
- The check never imports GUI or pipeline code, so it stays fast and cannot
  flake on external state.

## Revision 2026-08-01: review of PR #27

Six findings from the review of the merged PR, all addressed in the follow-up:

1. **The path scan was untested.** Its skip rules, allowlist and base
   resolution sat inline in the test body, so a broadened skip would have made
   it pass on everything silently. Extracted to `unresolved_paths()` and
   pinned against seeded drift — see "Testing the checker" above. This is also
   what makes the original doc's claim that each check was demonstrated on
   seeded drift true of check 1, which it had not been.
2. **An unclosed fence passed vacuously.** One stray fence line and every path
   below it went unscanned while the check still reported success —
   contradicting the "fail loudly, never vacuously" stance the two table
   parsers already took. `has_unclosed_fence()` now makes it a failure, and
   the scan additionally fails if it resolved *no* candidates at all.
3. **GUI routes were false failures.** `/watches/` satisfies the path-candidate
   test, resolves against nothing, and would have been reported with the wrong
   advice ("fix the doc"). It only never bit because the routes in the docs are
   written `/papers/<id>`, whose angle brackets fail the charset by accident.
   A leading `/` is now skipped deliberately, alongside `bmlib/` and `~`.
4. **`CLAUDE.md` held a third, unguarded copy of the listing** — see Scope.
5. **Case sensitivity** is the filesystem's, so a wrong-case path passes on
   macOS and fails on Linux CI. Accepted, and now noted in the docstring so
   the asymmetry is not a surprise.
6. `documented_migrations()` no longer assumes the `|---|` separator sits
   immediately below the header; it skips separator rows wherever they are.
   The old form failed safe (a mismatch, not a false pass), so this is tidying.

Deliberately **not** changed: check 1 still does not run over `CLAUDE.md`'s
backticked paths — that is the widening that needs a wider allowlist, and the
Scope argument above still holds against it.

## Revision 2026-08-02: unambiguous failure lines (issue #30)

`unresolved_paths()` reported `f"{doc.name}:{line_no}"`, which cannot say which
file to fix: `index.md` exists in **both** `docs/dev/` and `docs/user/`, and
`CLAUDE.md` is a third scannable file. Failure lines now carry the path
relative to the repo root, via `doc_label()`, falling back to the bare name for
a file outside the repo — which is what the fixtures scan, and what has no
repo-relative form to report. The unclosed-fence line uses the same label, and
is pinned separately (patching a stand-in repo root, since a fixture file
otherwise has no relative form) so a partial revert cannot pass.

**The glob was deliberately not widened, and issue #30 closes as won't-fix on
that half.** Two reasons, the second being the one that matters:

1. `docs/user/*.md` yields **zero** path candidates — measured, not assumed.
   Its `~/.bmnews/config.toml` references fail the charset on `~` before
   `SKIPPED_PREFIXES` is even consulted, and the rest are CLI commands. So
   scanning it would check nothing today.
2. Folding a permanently-empty tree into the scan **weakens the
   `assert scan.checked` guard**, which is the module's central no-vacuous-pass
   property. The guard is an aggregate: `docs/dev/` alone keeps it non-zero, so
   the `docs/user/` half could stop being scanned — a bad glob, a renamed
   directory — with nothing to notice. Future-proofing that blinds a live guard
   is a bad trade for zero present coverage.

The format fix has standalone value regardless, which is why it was worth doing
without the widening: every scan failure now names its file unambiguously.

Worth knowing if this is revisited: **`CLAUDE.md` yields 27 path candidates and
all 27 resolve against the current bases and allowlist**. The Scope argument
above — that widening needs a wider allowlist — is therefore not true of
`CLAUDE.md` as it stands today, whatever else argues for leaving it out. That
is a fresh design conversation, not a follow-on from this change.
