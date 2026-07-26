"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.backends import backend_params, use_backend


@pytest.fixture(params=backend_params())
def db_backend(request: pytest.FixtureRequest) -> Iterator[str]:
    """Run the requesting test once per supported database backend.

    A module opts in with ``pytestmark = pytest.mark.usefixtures("db_backend")``;
    every test in it is then parameterised, and the ``_db()`` helpers build
    their databases on whichever backend the current run selected. See
    ``tests/backends.py`` for why the selection is process state.
    """
    with use_backend(request.param) as backend:
        yield backend
