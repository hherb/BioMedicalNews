"""Drift checks for docs/dev: the manual must fail CI when it stops matching the code.

Exact-match checks only (issue #16's first pass): backticked repo paths must
exist, the migration table in database.md must match MIGRATIONS, and the
test-file listing in testing.md must match tests/. The parsers are module-level
functions tested against literal fixture strings below; the TestDocsMatchCode
checks then run them against the real tree.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from bmnews.db.migrations import MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DEV = REPO_ROOT / "docs" / "dev"

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


# Worked examples the docs tell a reader to create; they do not exist by design.
# Anything added here needs the same justification. `newsource.py` is the
# add-a-fetcher example in contributing.md and bmlib-integration.md.
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


class TestPathBases:
    def test_includes_root_package_and_subpackages(self):
        bases = path_bases()
        assert REPO_ROOT in bases
        assert REPO_ROOT / "bmnews" in bases
        # Computed from the tree, so the docs' package-relative shorthand
        # (`db/operations.py`, `channels/`) resolves without per-token rules.
        assert REPO_ROOT / "bmnews" / "db" in bases
        assert REPO_ROOT / "bmnews" / "notify" in bases


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
