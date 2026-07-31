# docs/dev Drift Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pytest module that fails CI when `docs/dev/` drifts from the code — stale paths, a migration table out of step with `MIGRATIONS`, or a test-file listing out of step with `tests/`.

**Architecture:** One new test module, `tests/test_docs.py`, in the ordinary suite (CI already runs pytest, so no new CI wiring). Module-level parser functions are unit-tested against literal fixture strings; three live checks then run the parsers against the real tree. Spec: `docs/plans/2026-08-01-docs-drift-check-design.md`.

**Tech Stack:** Python 3.11+, pytest, stdlib only (`re`, `pathlib`). Imports `bmnews.db.migrations.MIGRATIONS`; no database, no LLM, no network.

## Global Constraints

- Python 3.11+ syntax; `from __future__ import annotations` at the top of the module.
- ruff: line-length 100, rules E, F, I, N, W, UP. Never name a variable `l` (E741).
- Type hints on all function signatures; Google-style docstrings on the parser functions.
- Run everything through `uv run …`; never bare `pip`.
- Conventional commit messages (`test:`, `docs:`).
- Exact-match checks only — no symbol-resolution heuristics (the user's explicit scope decision).
- Only `docs/dev/*.md` is scanned; `CLAUDE.md`, `HANDOVER.md`, `docs/user/` are out of scope for the *check* (though task 4 updates CLAUDE.md's test-file table as ordinary doc upkeep).

---

### Task 1: Inline-code extraction and the path-candidate filter

**Files:**
- Create: `tests/test_docs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `iter_inline_code(text: str) -> Iterator[tuple[int, str]]` (yields `(line_number, token)` for inline backticked code outside fenced blocks, line numbers 1-based) and `is_path_candidate(token: str) -> bool`. Also the module constants `REPO_ROOT: Path` and `DOCS_DEV: Path` that tasks 2–4 use.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_docs.py` with the module docstring, imports, constants, and the first two test classes. The functions under test do not exist yet.

````python
"""Drift checks for docs/dev: the manual must fail CI when it stops matching the code.

Exact-match checks only (issue #16's first pass): backticked repo paths must
exist, the migration table in database.md must match MIGRATIONS, and the
test-file listing in testing.md must match tests/. The parsers are module-level
functions tested against literal fixture strings below; the Test*MatchesCode
checks then run them against the real tree.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from bmnews.db.migrations import MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DEV = REPO_ROOT / "docs" / "dev"


class TestIterInlineCode:
    def test_yields_tokens_with_line_numbers(self):
        text = "first `a/b.py` and `c/d.py`\nsecond `e.py`\n"
        assert list(iter_inline_code(text)) == [(1, "a/b.py"), (1, "c/d.py"), (2, "e.py")]

    def test_skips_fenced_blocks(self):
        text = (
            "before `real/path.py`\n"
            "```python\n"
            "code = fetch(`fenced/example.py`)\n"
            "```\n"
            "after `other/path.md`\n"
        )
        assert list(iter_inline_code(text)) == [(1, "real/path.py"), (5, "other/path.md")]


class TestIsPathCandidate:
    def test_accepts_files_and_directories(self):
        assert is_path_candidate("bmnews/cli.py")
        assert is_path_candidate("bmnews/notify/")
        assert is_path_candidate("docs/plans/2026-08-01-docs-drift-check-design.md")

    def test_rejects_non_paths(self):
        assert not is_path_candidate("conftest.py")  # no slash: bare filenames are prose
        assert not is_path_candidate("n-1/n")  # slash, but no path-like ending
        assert not is_path_candidate("https://api.example.org/search")  # ':' fails the charset
        assert not is_path_candidate("provider:model")  # no slash and ':' anyway
````

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_docs.py -v`
Expected: FAIL — `NameError: name 'iter_inline_code' is not defined` (and the same for `is_path_candidate`).

- [ ] **Step 3: Implement the two functions**

Insert between the constants and the test classes:

````python
_INLINE_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^\s*(```|~~~)")
_PATH_CHARS = re.compile(r"[A-Za-z0-9_./-]+")
_PATH_EXTENSIONS = (".py", ".md", ".toml", ".txt", ".html", ".css", ".js", ".json")


def iter_inline_code(text: str) -> Iterator[tuple[int, str]]:
    """Yield (line_number, token) for inline backticked code outside fenced blocks.

    Args:
        text: Full markdown source.

    Yields:
        1-based line number and the token between single backticks. Fenced
        code blocks hold example code, not references, so they are skipped.
    """
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _INLINE_CODE.finditer(line):
            yield line_no, match.group(1)


def is_path_candidate(token: str) -> bool:
    """Whether a backticked token claims to be a repo path.

    A candidate is made only of path characters (URLs contain ':' and fail),
    contains a '/', and ends with '/' or a known file extension — which is
    what separates `bmnews/cli.py` from prose fragments like `n-1/n`.
    """
    if not _PATH_CHARS.fullmatch(token):
        return False
    if "/" not in token:
        return False
    return token.endswith("/") or token.endswith(_PATH_EXTENSIONS)
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_docs.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_docs.py
git commit -m "test: inline-code extraction and path-candidate filter for the docs drift check"
```

---

### Task 2: The backticked-paths live check

**Files:**
- Modify: `tests/test_docs.py`
- Possibly modify: any `docs/dev/*.md` file the new check catches a genuinely stale path in

**Interfaces:**
- Consumes: `iter_inline_code`, `is_path_candidate`, `REPO_ROOT`, `DOCS_DEV` from Task 1.
- Produces: `path_bases() -> list[Path]` and `KNOWN_FICTIONAL_PATHS: frozenset[str]`; the live check `TestDocsMatchCode.test_backticked_paths_exist`.

- [ ] **Step 1: Write the failing test for `path_bases`**

Add to `tests/test_docs.py`:

````python
class TestPathBases:
    def test_includes_root_package_and_subpackages(self):
        bases = path_bases()
        assert REPO_ROOT in bases
        assert REPO_ROOT / "bmnews" in bases
        # Computed from the tree, so the docs' package-relative shorthand
        # (`db/operations.py`, `channels/`) resolves without per-token rules.
        assert REPO_ROOT / "bmnews" / "db" in bases
        assert REPO_ROOT / "bmnews" / "notify" in bases
````

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_docs.py::TestPathBases -v`
Expected: FAIL — `NameError: name 'path_bases' is not defined`.

- [ ] **Step 3: Implement `path_bases`, the allowlist, and the live check**

Insert after `is_path_candidate`:

````python
# Worked examples the docs tell a reader to create; they do not exist by design.
# Anything added here needs the same justification.
KNOWN_FICTIONAL_PATHS = frozenset({"bmnews/fetchers/newsource.py"})


def path_bases() -> list[Path]:
    """Directories a documented path may be relative to.

    The repo root, ``bmnews/``, and every direct subpackage of ``bmnews/`` —
    computed from the tree, not hardcoded, so a new subpackage needs no edit
    here. This resolves the docs' package-relative shorthand.
    """
    package_root = REPO_ROOT / "bmnews"
    bases = [REPO_ROOT, package_root]
    for child in sorted(package_root.iterdir()):
        if child.is_dir() and (child / "__init__.py").exists():
            bases.append(child)
    return bases
````

Add the live-check class at the bottom of the module (tasks 3 and 4 add to it):

````python
class TestDocsMatchCode:
    """The three live checks: docs/dev against the real tree."""

    def test_backticked_paths_exist(self):
        bases = path_bases()
        failures = []
        for doc in sorted(DOCS_DEV.glob("*.md")):
            for line_no, token in iter_inline_code(doc.read_text(encoding="utf-8")):
                if not is_path_candidate(token):
                    continue
                if token.startswith(("bmlib/", "~")):
                    continue  # a different repo; a user-home runtime file
                if token in KNOWN_FICTIONAL_PATHS:
                    continue
                if not any((base / token).exists() for base in bases):
                    failures.append(f"{doc.name}:{line_no}: `{token}`")
        assert not failures, (
            "docs/dev references paths that do not exist — fix the doc, or add a "
            "worked example to KNOWN_FICTIONAL_PATHS:\n" + "\n".join(failures)
        )
````

- [ ] **Step 4: Run the whole module**

Run: `uv run pytest tests/test_docs.py -v`
Expected: `TestPathBases` and the fixture tests pass. `test_backticked_paths_exist` **either** passes **or** fails listing real stale paths. A failure here is the check earning its keep: for each listed token decide —
- genuinely stale (the file moved or was renamed): fix the doc reference in the same commit;
- a worked example that never exists (like `newsource.py`): add it to `KNOWN_FICTIONAL_PATHS` with a comment saying which doc uses it and why.

Do **not** loosen `is_path_candidate` to make a failure go away; the filter is the spec's, and a token it admits is either a real path or doc rot.

- [ ] **Step 5: Rerun until green, then commit**

Run: `uv run pytest tests/test_docs.py -v`
Expected: all pass.

```bash
git add tests/test_docs.py docs/dev/
git commit -m "test: docs/dev backticked paths must exist in the tree"
```

---

### Task 3: The migration-table check

**Files:**
- Modify: `tests/test_docs.py`
- Possibly modify: `docs/dev/database.md` (only if the live check exposes real drift)

**Interfaces:**
- Consumes: `DOCS_DEV` from Task 1; `MIGRATIONS` (already imported).
- Produces: `documented_migrations(text: str) -> set[tuple[int, str]] | None`; the live check `TestDocsMatchCode.test_migration_table_matches_migrations`.

- [ ] **Step 1: Write the failing parser tests**

````python
MIGRATION_DOC = """\
## Migrations

| # | Name | What it does |
|---|------|--------------|
| 1 | `initial_schema` | The original tables |
| 2 | `add_paper_tags` | `paper_tags` |

Prose after the table must not be parsed as rows.
"""


class TestDocumentedMigrations:
    def test_parses_version_name_pairs_and_stops_at_table_end(self):
        assert documented_migrations(MIGRATION_DOC) == {
            (1, "initial_schema"),
            (2, "add_paper_tags"),
        }

    def test_returns_none_without_the_header(self):
        other_table = "| Column | Type |\n|---|---|\n| `id` | INTEGER |\n"
        assert documented_migrations(other_table) is None
````

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_docs.py::TestDocumentedMigrations -v`
Expected: FAIL — `NameError: name 'documented_migrations' is not defined`.

- [ ] **Step 3: Implement the parser**

Insert after `path_bases`:

````python
_MIGRATION_HEADER = re.compile(r"^\|\s*#\s*\|\s*Name\s*\|")
_MIGRATION_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|")


def documented_migrations(text: str) -> set[tuple[int, str]] | None:
    """Parse (version, name) pairs from database.md's migration table.

    Anchored on the ``| # | Name |`` header; rows are read until the first
    line that is not a migration row. Returns None when no such table exists,
    so the caller fails loudly rather than passing vacuously.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _MIGRATION_HEADER.match(line):
            pairs = set()
            for row in lines[index + 2 :]:  # skip the |---|---| separator line
                match = _MIGRATION_ROW.match(row)
                if not match:
                    break
                pairs.add((int(match.group(1)), match.group(2)))
            return pairs
    return None
````

- [ ] **Step 4: Run to verify the parser tests pass**

Run: `uv run pytest tests/test_docs.py::TestDocumentedMigrations -v`
Expected: 2 passed.

- [ ] **Step 5: Add the live check**

Append to `TestDocsMatchCode`:

````python
    def test_migration_table_matches_migrations(self):
        text = (DOCS_DEV / "database.md").read_text(encoding="utf-8")
        documented = documented_migrations(text)
        assert documented is not None, (
            "migration table (header `| # | Name |`) not found in database.md — "
            "renaming the header is itself drift"
        )
        actual = {(m.version, m.name) for m in MIGRATIONS}
        missing_from_doc = actual - documented
        gone_from_code = documented - actual
        assert documented == actual, (
            f"database.md's migration table is out of step with MIGRATIONS — "
            f"in code but not documented: {sorted(missing_from_doc)}; "
            f"documented but not in code: {sorted(gone_from_code)}"
        )
````

- [ ] **Step 6: Run the whole module**

Run: `uv run pytest tests/test_docs.py -v`
Expected: all pass — `database.md` currently documents migrations 1–7, which matches code. If it fails, the doc drifted since this plan was written: update the doc's table, never the check.

- [ ] **Step 7: Commit**

```bash
git add tests/test_docs.py docs/dev/database.md
git commit -m "test: database.md migration table must match MIGRATIONS"
```

---

### Task 4: The test-file-listing check

**Files:**
- Modify: `tests/test_docs.py`, `docs/dev/testing.md` (the listing gains `test_docs.py`), `CLAUDE.md` (test-file table row + `tests/` file count, ordinary upkeep)

**Interfaces:**
- Consumes: `DOCS_DEV`, `REPO_ROOT` from Task 1.
- Produces: `documented_test_files(text: str) -> set[str] | None`; the live check `TestDocsMatchCode.test_test_file_listing_matches_tests_dir`.

- [ ] **Step 1: Write the failing parser tests**

The fixture contains a fenced block, so it lives most readably in a module-level constant:

````python
TESTS_DOC = """\
## Test structure

```
tests/
  backends.py         # Not a test: helper
  test_cli.py         # The CLI
  test_db.py          # Every DB operation
```
"""


class TestDocumentedTestFiles:
    def test_extracts_py_names_from_the_tests_block(self):
        assert documented_test_files(TESTS_DOC) == {"backends.py", "test_cli.py", "test_db.py"}

    def test_returns_none_without_the_block(self):
        assert documented_test_files("no fenced block here\n") is None

    def test_ignores_fenced_blocks_that_are_not_the_listing(self):
        text = "```\nsome other example\n```\n\n" + TESTS_DOC
        assert documented_test_files(text) == {"backends.py", "test_cli.py", "test_db.py"}
````

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_docs.py::TestDocumentedTestFiles -v`
Expected: FAIL — `NameError: name 'documented_test_files' is not defined`.

- [ ] **Step 3: Implement the parser**

Insert after `documented_migrations`:

````python
def documented_test_files(text: str) -> set[str] | None:
    """Extract the *.py names from testing.md's fenced ``tests/`` listing.

    The listing is the fenced code block whose first non-blank line is
    exactly ``tests/``. Trailing comments are ignored. Returns None when no
    such block exists, so the caller fails loudly rather than vacuously.
    """
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and lines[0] == "tests/":
            names = (line.split()[0] for line in lines[1:])
            return {name for name in names if name.endswith(".py")}
    return None
````

- [ ] **Step 4: Run to verify the parser tests pass**

Run: `uv run pytest tests/test_docs.py::TestDocumentedTestFiles -v`
Expected: 3 passed.

- [ ] **Step 5: Add the live check**

Append to `TestDocsMatchCode`:

````python
    def test_test_file_listing_matches_tests_dir(self):
        text = (DOCS_DEV / "testing.md").read_text(encoding="utf-8")
        documented = documented_test_files(text)
        assert documented is not None, (
            "fenced `tests/` listing not found in testing.md — "
            "removing the block is itself drift"
        )
        actual = {p.name for p in (REPO_ROOT / "tests").glob("*.py")} - {"__init__.py"}
        undocumented = actual - documented
        gone_from_tree = documented - actual
        assert documented == actual, (
            f"testing.md's test-file listing is out of step with tests/ — "
            f"in tests/ but not documented: {sorted(undocumented)}; "
            f"documented but not in tests/: {sorted(gone_from_tree)}"
        )
````

- [ ] **Step 6: Run it and watch it fail for the right reason**

Run: `uv run pytest tests/test_docs.py::TestDocsMatchCode::test_test_file_listing_matches_tests_dir -v`
Expected: FAIL — `in tests/ but not documented: ['test_docs.py']`. The module being written is itself undocumented; this failure is the check working end to end on real drift.

- [ ] **Step 7: Update the docs, rerun, and verify green**

In `docs/dev/testing.md`, add to the fenced `tests/` block, in alphabetical position (between `test_digest.py` and `test_fetchers.py`):

```
  test_docs.py        # docs/dev drift: paths exist, migration + test tables match
```

In `CLAUDE.md`: add a row to the test-file table, alphabetically between `test_digest.py` and `test_fetchers.py`:

```
| `test_docs.py` | The docs/dev drift backstop — backticked repo paths exist (allowlist for worked examples like `newsource.py`), `database.md`'s migration table matches `MIGRATIONS`, `testing.md`'s test listing matches `tests/`; parsers pinned against fixture strings |
```

and correct the `tests/` line in the directory structure to the real count (17 test files after this change — it says 14, which was already stale).

Run: `uv run pytest tests/test_docs.py -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add tests/test_docs.py docs/dev/testing.md CLAUDE.md
git commit -m "test: testing.md's test-file listing must match tests/"
```

---

### Task 5: Full suite, lint, handover, PR

**Files:**
- Modify: `HANDOVER.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a green branch and a PR closing #16.

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: everything passes. `db/operations.py` and `db/migrations.py` were not touched, so the PostgreSQL half is not required for this branch.

- [ ] **Step 2: Lint and format**

Run: `uv run ruff check bmnews/ tests/ && uv run ruff format --check bmnews/ tests/`
Expected: clean. If `ruff format` wants changes in `tests/test_docs.py`, apply with `uv run ruff format tests/test_docs.py` and rerun.

- [ ] **Step 3: Update HANDOVER.md**

In the "Where things stand" table, change the `docs/dev` drift detection row to **Done** with a one-line description (pytest backstop in `tests/test_docs.py`; symbol-resolution half deliberately deferred — new issue only if wanted later). Add a short section noting the three checks and where the allowlist lives, so the next session knows `KNOWN_FICTIONAL_PATHS` is the knob when a doc adds a worked example.

- [ ] **Step 4: Commit and push**

```bash
git add HANDOVER.md
git commit -m "docs: record the docs/dev drift backstop in the handover"
git push -u origin test/docs-drift-check
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "test: CI backstop for docs/dev drift" --body "..."
```

Body: what the three checks cover, the allowlist mechanism, the deliberate exact-match-only scope, `Closes #16`, and the Claude Code attribution footer.
