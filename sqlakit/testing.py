"""Helpers a test needs of a database, beyond a rollback and a schema."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from . import db as sync_db
from ._recording import Recording, check, require_expectation
from .asyncio import db as async_db
from .exceptions import UnknownDatabaseError
from .types import AssertQueries

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["AssertQueries", "assert_queries"]


@contextmanager
def assert_queries(
    count: int | None = None,
    *,
    at_most: int | None = None,
    duplicates: bool = True,
    using: str | Any = None,  # noqa: ANN401
) -> Generator[Recording]:
    """Assert what the block asks of the database.

    ```python
    with assert_queries(2):
        User.query.order_by("name").page(limit=10)

    with assert_queries(at_most=5):
        render(dashboard)

    with assert_queries(duplicates=False):
        render(users)  # the N+1 test, without a number
    ```

    The three checks stand alone or together: a count, a ceiling, and whether a
    statement may run twice. What fails prints the statements, numbered and
    timed, with the repeated ones pointing at each other.

    ``using`` is the database to watch, as an alias or as the database itself.
    Left out, it watches the importable registries, `sqlakit.db` and
    `sqlakit.asyncio.db`, and every database each of them has. Awaited work is
    watched the same way, so the block stays `with`.

    Transaction control is not counted: `BEGIN` and `COMMIT` reach a cursor on
    some drivers and not others.

    Raises:
        TypeError: if there is nothing to assert, or nothing to watch.
        UnknownDatabaseError: if no configured registry has that alias.

    """
    require_expectation(count, at_most, duplicates)

    with ExitStack() as stack:
        recording = Recording()
        for db in _watched(using):
            stack.enter_context(db.recording(into=recording))
        yield recording

    check(recording, count=count, at_most=at_most, duplicates=duplicates)


def _watched(using: str | Any) -> list[Any]:  # noqa: ANN401
    """Return the databases to record: the one named, or every configured one."""
    if using is None:
        return _configured()
    if not isinstance(using, str):
        return [using]
    registries = _configured()
    watched = [registry[using] for registry in registries if using in registry]
    if not watched:
        known = {alias for registry in registries for alias in registry.aliases}
        raise UnknownDatabaseError(using, tuple(sorted(known)))
    return watched


def _configured() -> list[Any]:
    """Return the importable registries an application has configured."""
    registries = [db for db in (sync_db, async_db) if db.is_configured]
    if not registries:
        message = (
            "assert_queries has no database to watch. Configure `sqlakit.db`, "
            "or name one with `assert_queries(..., using=db)`."
        )
        raise TypeError(message)
    return registries
