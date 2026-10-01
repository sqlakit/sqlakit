from __future__ import annotations

import functools
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, TypeVar, cast, overload

from ._base import _DatabaseRegistryMixin, default_backoff, late_bound
from ._db import Database, RetryingTransaction

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ._base import RetryOn
    from ._db import Transaction

__all__ = ["Databases", "autocommit", "db", "transaction"]

_FuncT = TypeVar("_FuncT", bound="Callable[..., Any]")


class Databases(_DatabaseRegistryMixin[Database], Database):
    """The databases an application talks to, and the default one among them.

    ```python
    from sqlakit import db

    db.configure(DB_URL)

    db.session  # the default database
    db["replica"].session  # another, if one was configured
    ```

    Use it when one database is enough and passing a handle around is not worth
    it. `Database(url)` is the alternative.
    """

    _database_class = Database

    @contextmanager
    def transactions(
        self,
        **arguments: Any,  # noqa: ANN401
    ) -> Iterator[None]:
        """Open a transaction on every database, not the default one alone.

        A single database needs `transaction(rollback=True)`. A test harness
        with several needs this:

        ```python
        with db.transactions(rollback=True):
            yield
        ```
        """
        with ExitStack() as stack:
            for alias in self.aliases:
                stack.enter_context(self[alias].transaction(**arguments))
            yield

    def dispose(self, *, close: bool = True) -> None:
        """Dispose of every configured database, not just the default one."""
        if self._built_its_own:
            super().dispose(close=close)
        elif self._default is not None:
            self._default.dispose(close=close)
        for db in self._aliased.values():
            db.dispose(close=close)


db = Databases()
"""The importable registry: one [`Databases`][sqlakit.Databases] for the process.

`db.configure(url)` fills it, and every module then reaches the same connections
by importing it. Reconfiguring is allowed until something connects, after which
it raises
[`DatabaseAlreadyConfiguredError`][sqlakit.DatabaseAlreadyConfiguredError]
and `dispose()` has to come first.
"""


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
    """Run the function in a transaction on a database found when it is called.

    [`Database.transaction`][sqlakit.Database.transaction] keeps the database it
    was called on, at import. This looks the database up on every call, so a
    test that swaps it, or an application that builds it after its modules are
    imported, reaches every decorated function:

    ```python
    from sqlakit import transaction


    @transaction
    def import_users() -> None: ...


    @transaction(using="replica")
    def rebuild_report() -> None: ...


    @transaction(using=lambda: container.resolve(Database))
    def create_user(name: str) -> None: ...
    ```

    A decorator only: a `with` block opens at once, so it reaches the database
    as late as this would.

    Args:
        func: The function to decorate, when used as a bare decorator.
        using: Where the transaction opens. Nothing is the default database of
            [`sqlakit.db`][sqlakit.db], a name is an alias in it, and a callable
            returns the database. A database is taken as it is, and a lazy
            reference to one is then resolved on each call.
        savepoint: As on `Database.transaction`.
        join_nested: As on `Database.transaction`.
        rollback: As on `Database.transaction`.
        commit_on_error: As on `Database.transaction`.
        retry_on: As on `Database.transaction`, which makes this return a
            [`RetryingTransaction`][sqlakit.RetryingTransaction]. Every attempt
            looks the database up again.
        max_retries: As on `Database.transaction`.
        backoff: As on `Database.transaction`.

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
    """Run the function in ``AUTOCOMMIT``, on a database found when it is called.

    [`Database.autocommit`][sqlakit.Database.autocommit], with the database
    looked up on every call as [`transaction`][sqlakit.transaction] does it:

    ```python
    from sqlakit import autocommit


    @autocommit(using="warehouse")
    def vacuum() -> None: ...
    ```

    Args:
        func: The function to decorate, when used as a bare decorator.
        using: Where the block opens, as `transaction` takes it.

    """

    def opened() -> Any:  # noqa: ANN401
        found: Database = late_bound(using, db)
        return found.autocommit()

    decorate: Callable[[_FuncT], _FuncT] = _opening(opened)
    if func is not None:
        return decorate(func)
    return decorate


def _opening(opened: Callable[[], Any]) -> Callable[[_FuncT], _FuncT]:
    """Return a decorator that runs the function in the block `opened` returns."""

    def decorate(func: _FuncT) -> _FuncT:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            with opened():
                return func(*args, **kwargs)

        return cast("_FuncT", wrapper)

    return decorate
