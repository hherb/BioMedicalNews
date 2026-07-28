---
name: fixall
description: Use when a code review has produced issues that need to be addressed and the pull request finalized on the bmnews project.
disable-model-invocation: true
allowed-tools: Read, Edit, Bash(git add *), Bash(git commit *), Bash(git push *), Bash(git status *), Bash(git diff *), Bash(gh issue *), Bash(gh pr *), Bash(uv run pytest *), Bash(ruff *), Bash(uv run ruff *)
---

Address all issues identified in the code review one by one. If fixing them appears manageable within this session, fix them now. If not, lodge the issue on GitHub. Once all issues have been addressed, run the test suite with `uv run pytest tests/ -v` plus `uv run ruff check bmnews/ tests/` and `uv run ruff format --check bmnews/ tests/` — and, if a fix touched `db/operations.py` or `db/migrations.py`, the PostgreSQL half as well (`BMNEWS_TEST_PG_DSN=... uv run pytest tests/test_db.py -v`), which skips silently without a DSN. Then review the code changes thoroughly against the coding conventions in CLAUDE.md (plus `bmnews/gui/CLAUDE.md` for anything under `bmnews/gui/`). If satisfied no issues are left open, update HANDOVER.md and the relevant `docs/plans/` document ONLY if necessary to reflect these changes. Then commit and push the changes into the PR.
