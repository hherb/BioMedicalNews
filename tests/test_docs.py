"""Drift checks for the developer docs: they must fail CI when they stop matching the code.

Exact-match checks only (issue #16's first pass): backticked repo paths in
``docs/dev/`` must exist, the migration table in database.md must match
MIGRATIONS, and both test-file listings — testing.md's and CLAUDE.md's — must
match ``tests/``. The parsers *and* the path scan are module-level functions
tested against literal fixtures below; the TestDocsMatchCode checks then run
them against the real tree.

Every parser returns None rather than an empty result when its anchor is
missing, and the scan reports an unclosed fence: a check that cannot find what
it is meant to compare must fail loudly, never pass vacuously.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from bmnews.db.migrations import MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DEV = REPO_ROOT / "docs" / "dev"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

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


def has_unclosed_fence(text: str) -> bool:
    """Whether a fence is still open at end of file.

    An odd number of fence lines makes :func:`iter_inline_code` swallow
    everything below the stray one, so the file would be scanned in part and
    still report no failures. The scan treats that as drift in its own right.

    Args:
        text: Full markdown source.

    Returns:
        True when the fence markers do not pair up.
    """
    return sum(1 for line in text.splitlines() if _FENCE.match(line)) % 2 == 1


def is_path_candidate(token: str) -> bool:
    """Whether a backticked token claims to be a repo path.

    Args:
        token: The text between a pair of single backticks.

    Returns:
        True when the token is made only of path characters (URLs contain ':'
        and fail), contains a '/', and ends with '/' or a known file
        extension — which is what separates `bmnews/cli.py` from prose
        fragments like `n-1/n`.
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

# Prefixes that look like repo paths but are not: a different repository (whose
# files would otherwise be checked against whatever bmlib version this machine
# resolved), user-home runtime files, and GUI routes like `/watches/` — leading
# slash, so "does it exist" is the wrong question and "fix the doc" the wrong advice.
SKIPPED_PREFIXES = ("bmlib/", "~", "/")


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


@dataclass(frozen=True)
class PathScan:
    """The result of scanning docs for backticked repo paths.

    Attributes:
        failures: Human-readable ``file:line: `token``` lines, one per problem.
        checked: How many path candidates were resolved. Zero means the
            scanner has stopped recognising paths, which is not a pass.
    """

    failures: list[str]
    checked: int


def unresolved_paths(
    docs: Iterable[Path],
    bases: Sequence[Path],
    allowlist: frozenset[str] = KNOWN_FICTIONAL_PATHS,
) -> PathScan:
    """Scan markdown files for backticked repo paths that resolve against nothing.

    Note that path resolution is only as case-sensitive as the filesystem: a
    wrong-case path passes on macOS and fails on Linux CI.

    Args:
        docs: Markdown files to scan.
        bases: Directories a documented path may be relative to.
        allowlist: Paths that do not exist by design.

    Returns:
        A :class:`PathScan` holding one failure line per unresolved token (and
        one per file with an unclosed fence), plus the number of candidates
        actually resolved.
    """
    failures: list[str] = []
    checked = 0
    for doc in sorted(docs):
        text = doc.read_text(encoding="utf-8")
        if has_unclosed_fence(text):
            failures.append(f"{doc.name}: unclosed code fence — every path below it goes unchecked")
        for line_no, token in iter_inline_code(text):
            if not is_path_candidate(token):
                continue
            if token.startswith(SKIPPED_PREFIXES):
                continue
            if token in allowlist:
                continue
            checked += 1
            if not any((base / token).exists() for base in bases):
                failures.append(f"{doc.name}:{line_no}: `{token}`")
    return PathScan(failures=failures, checked=checked)


_TABLE_SEPARATOR = re.compile(r"^\|[\s\-:|]+\|\s*$")
_MIGRATION_HEADER = re.compile(r"^\|\s*#\s*\|\s*Name\s*\|")
_MIGRATION_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|")
_TEST_TABLE_HEADER = re.compile(r"^\|\s*File\s*\|")
_TEST_COUNT = re.compile(r"# Test suite \((\d+) test modules\)")


def documented_migrations(text: str) -> set[tuple[int, str]] | None:
    """Parse (version, name) pairs from database.md's migration table.

    Anchored on the ``| # | Name |`` header; the ``|---|`` separator is
    skipped and rows are read until the first line that is not a migration
    row. Returns None when no such table exists, so the caller fails loudly
    rather than passing vacuously.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _MIGRATION_HEADER.match(line):
            pairs = set()
            for row in lines[index + 1 :]:
                if _TABLE_SEPARATOR.match(row):
                    continue
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


def tabulated_test_files(text: str) -> set[str] | None:
    """Extract the *.py names from CLAUDE.md's test-file table.

    Anchored on the ``| File |`` header; only each row's **first** cell is
    read, so a filename mentioned in a coverage description cannot pass a file
    off as documented. One cell may name several files (``backends.py`` /
    ``conftest.py``). Returns None when no such table exists.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _TEST_TABLE_HEADER.match(line):
            continue
        names: set[str] = set()
        for row in lines[index + 1 :]:
            if _TABLE_SEPARATOR.match(row):
                continue
            if not row.startswith("|"):
                break
            first_cell = row.split("|")[1]
            names.update(t for t in _INLINE_CODE.findall(first_cell) if t.endswith(".py"))
        return names
    return None


def actual_test_files() -> set[str]:
    """The test-suite filenames both listings are checked against (``__init__.py`` aside)."""
    return {path.name for path in (REPO_ROOT / "tests").glob("*.py")} - {"__init__.py"}


def documented_test_count(text: str) -> int | None:
    """Read the test-module count from CLAUDE.md's directory tree.

    Returns None when the ``# Test suite (N test modules)`` comment is absent,
    so rewording it fails rather than silently retiring the check.
    """
    match = _TEST_COUNT.search(text)
    return int(match.group(1)) if match else None


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


class TestHasUnclosedFence:
    def test_balanced_fences_are_fine(self):
        assert not has_unclosed_fence("text\n```\ncode\n```\nmore `a/b.py`\n")

    def test_stray_fence_is_reported(self):
        # Without this, `c/d.py` below the stray fence is never scanned.
        assert has_unclosed_fence("`a/b.py`\n```\nstuff\n\n`c/d.py`\n")


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


class TestUnresolvedPaths:
    """The scan itself, on seeded drift — not just the parsers it calls."""

    def _doc(self, tmp_path: Path, body: str) -> Path:
        doc = tmp_path / "seeded.md"
        doc.write_text(body, encoding="utf-8")
        return doc

    def test_reports_only_the_missing_path_with_file_and_line(self, tmp_path):
        doc = self._doc(tmp_path, "real `bmnews/cli.py`\nmade up `bmnews/nope.py`\n")
        scan = unresolved_paths([doc], [REPO_ROOT])
        assert scan.failures == ["seeded.md:2: `bmnews/nope.py`"]
        assert scan.checked == 2

    def test_passes_clean_docs(self, tmp_path):
        doc = self._doc(tmp_path, "see `bmnews/cli.py` and `tests/test_docs.py`\n")
        assert unresolved_paths([doc], [REPO_ROOT]) == PathScan(failures=[], checked=2)

    def test_skips_fenced_examples(self, tmp_path):
        doc = self._doc(tmp_path, "```\n`bmnews/nope.py`\n```\n")
        assert unresolved_paths([doc], [REPO_ROOT]) == PathScan(failures=[], checked=0)

    def test_skips_other_repos_home_paths_and_routes(self, tmp_path):
        doc = self._doc(tmp_path, "`bmlib/db.py` `~/.bmnews/config.toml` `/watches/`\n")
        assert unresolved_paths([doc], [REPO_ROOT]) == PathScan(failures=[], checked=0)

    def test_skips_the_allowlist(self, tmp_path):
        doc = self._doc(tmp_path, "create `bmnews/fetchers/newsource.py`\n")
        assert unresolved_paths([doc], [REPO_ROOT]).failures == []
        assert unresolved_paths([doc], [REPO_ROOT], allowlist=frozenset()).failures == [
            "seeded.md:1: `bmnews/fetchers/newsource.py`"
        ]

    def test_unclosed_fence_is_a_failure(self, tmp_path):
        doc = self._doc(tmp_path, "`bmnews/cli.py`\n```\nstuff\n\n`bmnews/nope.py`\n")
        scan = unresolved_paths([doc], [REPO_ROOT])
        assert scan.failures == [
            "seeded.md: unclosed code fence — every path below it goes unchecked"
        ]


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

    def test_reads_the_first_row_without_a_separator(self):
        text = "| # | Name |\n| 1 | `initial_schema` |\n"
        assert documented_migrations(text) == {(1, "initial_schema")}

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


TEST_TABLE_DOC = """\
Test files:
| File | Coverage |
|---|---|
| `test_cli.py` | The CLI, which `test_db.py` says nothing about |
| `backends.py` / `conftest.py` | Not tests: fixtures |

Prose after the table must not be parsed as rows.
"""


class TestTabulatedTestFiles:
    def test_reads_first_cells_only(self):
        # `test_db.py` appears in a description, which must not document it.
        assert tabulated_test_files(TEST_TABLE_DOC) == {
            "test_cli.py",
            "backends.py",
            "conftest.py",
        }

    def test_returns_none_without_the_header(self):
        assert tabulated_test_files("| Section | Dataclass |\n|---|---|\n| `[llm]` | x |\n") is None


class TestDocumentedTestCount:
    def test_reads_the_count(self):
        assert documented_test_count("tests/    # Test suite (17 test modules)\n") == 17

    def test_returns_none_when_reworded(self):
        assert documented_test_count("tests/    # The test suite\n") is None


class TestDocsMatchCode:
    """The live checks: the docs against the real tree."""

    def test_backticked_paths_exist(self):
        scan = unresolved_paths(DOCS_DEV.glob("*.md"), path_bases())
        assert not scan.failures, (
            "docs/dev references paths that do not exist — fix the doc, or add a "
            "worked example to KNOWN_FICTIONAL_PATHS:\n" + "\n".join(scan.failures)
        )
        assert scan.checked, "no path candidates found at all — the scanner has stopped seeing them"

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
            "fenced `tests/` listing not found in testing.md — removing the block is itself drift"
        )
        actual = actual_test_files()
        assert documented == actual, (
            f"testing.md's test-file listing is out of step with tests/ — "
            f"in tests/ but not documented: {sorted(actual - documented)}; "
            f"documented but not in tests/: {sorted(documented - actual)}"
        )

    def test_claude_md_test_table_matches_tests_dir(self):
        text = CLAUDE_MD.read_text(encoding="utf-8")
        documented = tabulated_test_files(text)
        assert documented is not None, (
            "test-file table (header `| File |`) not found in CLAUDE.md — "
            "renaming the header is itself drift"
        )
        actual = actual_test_files()
        assert documented == actual, (
            f"CLAUDE.md's test-file table is out of step with tests/ — "
            f"in tests/ but not documented: {sorted(actual - documented)}; "
            f"documented but not in tests/: {sorted(documented - actual)}"
        )

    def test_claude_md_test_count_matches_tests_dir(self):
        documented = documented_test_count(CLAUDE_MD.read_text(encoding="utf-8"))
        assert documented is not None, (
            "`# Test suite (N test modules)` not found in CLAUDE.md's tree — "
            "rewording it is itself drift"
        )
        actual = len([name for name in actual_test_files() if name.startswith("test_")])
        assert documented == actual, (
            f"CLAUDE.md says {documented} test modules; tests/ holds {actual}"
        )
