---
name: nextsession
description: Use when starting or resuming a work session on the bmnews project, to load current project state and re-establish the coding rules and session workflow before doing any work.
disable-model-invocation: true
allowed-tools: Read, Edit, Bash(git add *), Bash(git commit *), Bash(git push *), Bash(git status *), Bash(git diff *), Bash(gh issue *), Bash(gh pr *), Bash(uv run pytest *), Bash(uv pip install *), Bash(ruff *), Bash(uv run ruff *)
---

read HANDOVER.md and follow the instructions. Ask me if you have any questions.

Our general coding rules live in CLAUDE.md — read and honour them (`bmnews/gui/CLAUDE.md` loads on top of it when the work is under `bmnews/gui/`). On top of those, follow this session workflow:

1. All tests must pass before committing, unless I explicitly give permission otherwise. Run the suite with `uv run pytest tests/ -v`, and lint with `uv run ruff check bmnews/ tests/` and `uv run ruff format --check bmnews/ tests/`. If you touched `db/operations.py` or `db/migrations.py`, run the PostgreSQL half too — `BMNEWS_TEST_PG_DSN=... uv run pytest tests/test_db.py -v` — since it silently skips without a DSN and the backend-specific SQL lives there.
2. Before you start working, make sure HANDOVER.md is up to date and represents the current state of progress, along with the design/plan documents in `docs/plans/` that cover the work in flight. If they are not, update them before you start.
3. Avoid technical debt — if you find an error, fix it when possible; otherwise lodge it as an issue on GitHub.
4. When you are done, update HANDOVER.md (and the relevant `docs/plans/` document) to reflect the current state of development and progress. Prune to stay concise and under 500 lines if possible: focus on what still needs doing, and summarise briefly what has already been done. If behaviour a user or a developer relies on has changed, update `docs/user/` and `docs/dev/` to match — and CLAUDE.md if the architecture description no longer holds. If you are not sure how to do this, ask me.
5. When the task is complete, commit all changes, push, and open a PR to the main branch. Link the PR to the relevant GitHub issue if applicable, and include a clear description of the changes made and any relevant context for reviewers. If you are not sure how to do this, ask me.
