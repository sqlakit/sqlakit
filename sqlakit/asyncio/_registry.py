from __future__ import annotations

import functools
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar, cast, overload

from sqlakit._base import _DatabaseRegistryMixin, default_backoff, late_bound

from ._db import Database, RetryingTransaction

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlakit._base import RetryOn

    from ._db import Transaction

__all__ = ["Databases", "autocommit", "db", "transaction"]

_FuncT = TypeVar("_FuncT", bound="Callable[..., Any]")


class Databases(_DatabaseRegistryMixin[Database], Database):
    """The databases an application talks to, and the default one among them.

    The asyncio counterpart of [`sqlakit.Databases`][sqlakit.Databases].

    ```python
    from sqlakit.asyncio import db

    db.configure(DB_URL)

    db.session  # the default database
    db["replica"].session  # another, if one was configured
    ```
    """

    _database_class = Database

    @asynccontextmanager
    async def transactions(
        self,
        **arguments: Any,  # noqa: ANN401
    ) -> AsyncIterator[None]:
        """Open a transaction on every database, not the default one alone.

        A test harness with more than one database needs this, where a single
        database needs `transaction(rollback=True)`.
        """
        async with AsyncExitStack() as stack:
            for alias in self.aliases:
                await stack.enter_async_context(self[alias].transaction(**arguments))
            yield

    async def dispose(self, *, close: bool = True) -> None:
        """Dispose of every configured database, not just the default one."""
        if self._built_its_own:
            await super().dispose(close=close)
        elif self._default is not None:
            await self._default.dispose(close=close)
        for db in self._aliased.values():
            await db.dispose(close=close)


db = Databases()
"""The database an application talks to. Configure it once, use it anywhere."""


@overload
def transaction(func: _FuncT, /) -> _FuncT: ...


@overload
def transaction(
    *,
    using: str | Database | Callable[[], Database] | None = None,
    savepoint: bool = False,
    join_nested: bool = True,
    rollback: bool = False,
    commit_on_error: type[BaseException]
    | tuple[type[BaseException], ...]
    | None = None,
    retry_on: None = None,
    max_retries: int = 3,
    backoff: Callable[[int], float] = default_backoff,
) -> Callable[[_FuncT], _FuncT]: ...


@overload
def transaction(
    *,
    using: str | Database | Callable[[], Database] | None = None,
    savepoint: bool = False,
    join_nested: bool = True,
    rollback: bool = False,
    commit_on_error: type[BaseException]
    | tuple[type[BaseException], ...]
    | None = None,
    retry_on: RetryOn,
    max_retries: int = 3,
    backoff: Callable[[int], float] = default_backoff,
) -> RetryingTransaction: ...


def transaction(  # noqa: PLR0913  (all keyword-only, as on `Database`)
    func: _FuncT | None = None,
    /,
    *,
    using: str | Database | Callable[[], Database] | None = None,
    savepoint: bool = False,
    join_nested: bool = True,
    rollback: bool = False,
    commit_on_error: type[BaseException]
    | tuple[type[BaseException], ...]
    | None = None,
    retry_on: RetryOn | None = None,
    max_retries: int = 3,
    backoff: Callable[[int], float] = default_backoff,
) -> _FuncT | Callable[[_FuncT], _FuncT] | RetryingTransaction:
    """Like `Database.transaction`, but find the database on every call.

    The asyncio version of [`sqlakit.transaction`][sqlakit.transaction].
    """

    def opened() -> Transaction:
        found: Database = late_bound(using, db)
        return found.transaction(
            savepoint=savepoint,
            join_nested=join_nested,
            rollback=rollback,
            commit_on_error=commit_on_error,
        )

    decorate: Callable[[_FuncT], _FuncT] | RetryingTransaction = (
        _opening(opened)
        if retry_on is None
        else RetryingTransaction(
            opened, retry_on=retry_on, max_retries=max_retries, backoff=backoff
        )
    )
    if func is not None:
        return decorate(func)
    return decorate


@overload
def autocommit(func: _FuncT, /) -> _FuncT: ...


@overload
def autocommit(
    *,
    using: str | Database | Callable[[], Database] | None = None,
) -> Callable[[_FuncT], _FuncT]: ...


def autocommit(
    func: _FuncT | None = None,
    /,
    *,
    using: str | Database | Callable[[], Database] | None = None,
) -> _FuncT | Callable[[_FuncT], _FuncT]:
    """Like `Database.autocommit`, but find the database on every call.

    The asyncio version of [`sqlakit.autocommit`][sqlakit.autocommit].
    """

    def opened() -> Any:  # noqa: ANN401
        found: Database = late_bound(using, db)
        return found.autocommit()

    decorate: Callable[[_FuncT], _FuncT] = _opening(opened)
    if func is not None:
        return decorate(func)
    return decorate


def _opening(opened: Callable[[], Any]) -> Callable[[_FuncT], _FuncT]:
    """Return a decorator that runs the function inside `opened()`."""

    def decorate(func: _FuncT) -> _FuncT:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            async with opened():
                return await func(*args, **kwargs)

        return cast("_FuncT", wrapper)

    return decorate
