from __future__ import annotations

import asyncio
import functools
import itertools
import logging
from contextlib import (
    AbstractAsyncContextManager,
    AsyncContextDecorator,
    AsyncExitStack,
    asynccontextmanager,
)
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast, overload

import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session
from sqlalchemy.util import await_only

from sqlakit._base import (
    BaseDatabase,
    BaseRetryingTransaction,
    _Lazy,
    _Scope,
    default_backoff,
    fix_sqlite_transactions,
    lazy_session_class,
)
from sqlakit.exceptions import MissingConnectionError, TransactionRolledBackError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
    from types import TracebackType

    from sqlalchemy.ext.asyncio import AsyncTransaction

    from sqlakit._base import RetryOn, _Scope

    from .orm import Query
    from .sql import SQL

ModelT = TypeVar("ModelT")
_FuncT = TypeVar("_FuncT", bound="Callable[..., Any]")

__all__ = ["Database", "RetryingTransaction", "Transaction"]

logger = logging.getLogger("sqlakit")


class Database(BaseDatabase[AsyncConnection, AsyncSession]):
    """The asyncio counterpart of [`sqlakit.Database`][sqlakit.Database].

    Scoping is the same: connections and sessions live in the context, tasks
    inherit them, transactions nest the same way. Every method that touches the
    database is awaited.
    """

    _engine: AsyncEngine | None = None
    _sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @cached_property
    def sql(self) -> SQL:
        """The SQL templates of this database.

        ```python
        db = Database(DB_URL, templates="app/sql")

        await db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
        ```

        """
        # Here rather than at the top, so that `import sqlakit` stays free of
        # the template engine until a template is asked for.
        from .sql import SQL  # noqa: PLC0415

        return SQL(self)

    def query(self, model: type[ModelT]) -> Query[ModelT]:
        """Build a query over a mapped class, on this database.

        ```python
        await db.query(User).where(User.is_active).order_by("name").page(limit=20)
        ```

        Any declarative class works, with no model layer under it. A model
        that has one reaches the same builder as `User.query`, on the database
        the model belongs to.
        """
        # Here rather than at the top: `orm` imports this module.
        from .orm import Query  # noqa: PLC0415

        return Query(model, self)

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine, created on first access."""
        if self._engine is None:
            with self._engine_lock:
                # Two threads reaching this at once would each build one, and
                # the pool of the loser would never be disposed.
                if self._engine is None:
                    self._engine = self._create_engine()
        return self._engine

    def _create_engine(self) -> AsyncEngine:
        engine = create_async_engine(self.url, **self.engine_args)
        fix_sqlite_transactions(engine.sync_engine)
        return engine

    @property
    def connection(self) -> AsyncConnection:
        """The connection bound to the current context.

        In a [`session_factory`][sqlakit.asyncio.Database.session_factory]
        block it raises until the session's first use: a property cannot await
        the checkout.

        Raises:
            MissingConnectionError: if no connection is bound.

        """
        scope = self._current_scope()
        if scope.connection is None:
            cell = scope.checkout
            if cell is not None and cell.connection is not None:
                scope.connection = cell.connection
            else:
                message = (
                    "No connection is open in this `session_factory()` block "
                    "yet. Use `db.session`, or open a `connect()` block if "
                    "you need the connection."
                )
                raise MissingConnectionError(message)
        return scope.connection

    async def _aconnection(self) -> AsyncConnection:
        """Return the bound connection, checking it out first if lazy."""
        return await self._areused(self._current_scope())

    def _create_session(self, connection: AsyncConnection) -> AsyncSession:
        if self._sessionmaker is None:
            self._sessionmaker = async_sessionmaker(**self.session_args)
        return self._sessionmaker(
            bind=connection,
            **self._session_args_for(connection),
        )

    def _lazy_session(self, cell: _Lazy[AsyncConnection]) -> AsyncSession:
        args: dict[str, Any] = dict(self.session_args)
        session_class = args.pop("class_", AsyncSession)
        args["sync_session_class"] = lazy_session_class(
            args.pop("sync_session_class", Session)
        )
        session = session_class(**args)

        def checkout() -> sa.Connection | None:
            # Already materialized, by a nested block: no greenlet needed.
            connection = cell.connection
            if connection is None:
                connection = await_only(cell.aget())
            return connection.sync_connection

        session.sync_session._sqlakit_checkout = checkout  # noqa: SLF001
        return session

    @staticmethod
    async def _areused(
        scope: _Scope[AsyncConnection, AsyncSession],
    ) -> AsyncConnection:
        """Return the scope's connection, checking it out first if lazy."""
        if scope.connection is None and scope.checkout is not None:
            scope.connection = await scope.checkout.aget()
        return cast("AsyncConnection", scope.connection)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """Open a connection and bind it, or reuse the one already bound."""
        elsewhere = self._stand_in()
        if elsewhere is not None:
            async with elsewhere.connect() as connection:
                yield connection
            return
        reuse = self._scope_to_reuse()
        if reuse is not None:
            async with self._bound(await self._areused(reuse)) as connection:
                yield connection
            return
        async with self.engine.connect() as opened, self._bound(opened) as connection:
            yield connection

    @overload
    def autocommit(self, func: _FuncT) -> _FuncT: ...

    @overload
    def autocommit(
        self,
        func: None = None,
    ) -> AbstractAsyncContextManager[AsyncConnection]: ...

    def autocommit(
        self,
        func: _FuncT | None = None,
    ) -> _FuncT | AbstractAsyncContextManager[AsyncConnection]:
        """Run in ``AUTOCOMMIT``, where every statement commits on its own.

        For read-only work, which has nothing to commit, and for statements that
        cannot run inside a transaction: ``VACUUM``, ``CREATE DATABASE``,
        ``CREATE INDEX CONCURRENTLY``. Inside a transaction it joins that one, since
        there is nothing else to commit into.

        A context manager and a decorator, with or without parentheses.

        Args:
            func: The function to decorate, when used as a bare decorator.

        """
        autocommit = self._autocommit()
        if func is not None:
            return autocommit(func)
        return autocommit

    @asynccontextmanager
    async def _autocommit(self) -> AsyncIterator[AsyncConnection]:
        """Open a connection in ``AUTOCOMMIT`` and bind it, or join the outer one."""
        elsewhere = self._stand_in()
        if elsewhere is not None:
            async with elsewhere.autocommit() as connection:
                yield connection
            return
        outer = self._connection_to_join()
        if outer is not None:
            async with self._bound(outer, commit=True) as connection:
                yield connection
            return
        async with self.engine.connect() as opened:
            await opened.execution_options(isolation_level="AUTOCOMMIT")
            with self._set_outer(None):
                async with self._bound(
                    opened, commit=True, autocommit=True
                ) as connection:
                    yield connection

    @overload
    def transaction(self, func: _FuncT) -> _FuncT: ...

    @overload
    def transaction(
        self,
        func: None = None,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
        retry_on: None = None,
        max_retries: int = 3,
        backoff: Callable[[int], float] = default_backoff,
    ) -> Transaction: ...

    @overload
    def transaction(
        self,
        func: None = None,
        *,
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

    def transaction(  # noqa: PLR0913  (all keyword-only; this is the main API)
        self,
        func: _FuncT | None = None,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
        retry_on: RetryOn | None = None,
        max_retries: int = 3,
        backoff: Callable[[int], float] = default_backoff,
    ) -> _FuncT | Transaction | RetryingTransaction:
        """Run a transaction on a connection bound to the current context.

        Commits when the block exits, rolls back if it raises. Inside another
        transaction it takes part in that one rather than opening a second
        connection: the outermost block commits.

        A context manager and a decorator, the latter with or without parentheses:

        ```python
        async with db.transaction():
            ...


        @db.transaction
        async def import_users() -> None: ...
        ```

        Args:
            func: The function to decorate, when used as a bare decorator.
            savepoint: Run as a savepoint when nested, so this block can fail and be
                rolled back on its own, and the blocks below it with it. Off by
                default: a savepoint costs a round trip.
            join_nested: Whether blocks below reuse this connection. Turn it off to
                let them reach the database on their own, seeing nothing of this
                block and surviving its rollback.
            rollback: Roll back on the way out rather than commit. Implies
                ``savepoint``, and wraps a test.
            commit_on_error: Exception types whose escape still commits. The
                exception propagates; what was written before it stays.
            retry_on: Exception types, or a predicate over the exception, worth
                another attempt. Decorator only: retrying re-runs the block, so this
                returns a [`RetryingTransaction`][sqlakit.asyncio.RetryingTransaction] that
                type checkers refuse to
                enter. Only the block that owns the transaction retries.
            max_retries: How many further attempts ``retry_on`` may buy.
            backoff: Seconds to wait before attempt ``n``, counted from zero.

        """

        def new_transaction() -> Transaction:
            return Transaction(
                self,
                savepoint=savepoint,
                join_nested=join_nested,
                rollback=rollback,
                commit_on_error=commit_on_error,
            )

        transaction: Transaction | RetryingTransaction = new_transaction()
        if retry_on is not None:
            transaction = RetryingTransaction(
                new_transaction,
                retry_on=retry_on,
                max_retries=max_retries,
                backoff=backoff,
            )
        if func is not None:
            return transaction(func)
        return transaction

    @asynccontextmanager
    async def session_factory(self) -> AsyncIterator[AsyncSession]:
        """Open a session for the block, and bind it.

        The session arrives at once, the connection on its first query or
        flush, as ``async_sessionmaker()`` does it. Inside another block it
        runs on the connection already bound.
        """
        elsewhere = self._stand_in()
        if elsewhere is not None:
            async with elsewhere.session_factory() as session:
                yield session
            return
        reuse = self._scope_to_reuse()
        if reuse is not None:
            async with self._bound(await self._areused(reuse)):
                yield self.session
            return
        # The lambda defers `self.engine` too: no engine until first use.
        cell: _Lazy[AsyncConnection] = _Lazy(lambda: self.engine.connect())  # noqa: PLW0108
        with self._bind(None, checkout=cell) as scope:
            try:
                yield self.session
            finally:
                try:
                    if scope.session is not None:
                        await scope.session.close()
                finally:
                    if cell.connection is not None:
                        await cell.connection.close()

    @asynccontextmanager
    async def _bound(
        self,
        connection: AsyncConnection,
        *,
        commit: bool = False,
        autocommit: bool = False,
    ) -> AsyncIterator[AsyncConnection]:
        """Bind ``connection`` for the block, ending the session it opened.

        ``commit`` keeps that session's work. Closing a session that isolates
        itself with a savepoint rolls back to it, discarding what the blocks
        below committed.
        """
        with self._bind(connection, autocommit=autocommit) as scope:
            done = False
            try:
                yield connection
                done = True
            finally:
                if scope.session is not None:
                    if commit and done:
                        await scope.session.commit()
                    await scope.session.close()

    @asynccontextmanager
    async def provisioned_tables(
        self,
        metadata: sa.MetaData,
        *,
        tables: Sequence[sa.Table] | None = None,
    ) -> AsyncIterator[None]:
        """Create these tables here, and drop them when the block ends.

        A test session opens this once, around everything that needs a schema:

        ```python
        @pytest.fixture(scope="session")
        async def tables():
            async with db.provisioned_tables(Model.metadata):
                yield
        ```

        Every table of the metadata unless ``tables`` names fewer, as a second
        database wants.
        """
        async with self.transaction() as connection:
            await connection.run_sync(metadata.create_all, tables=tables)
        try:
            yield
        finally:
            async with self.transaction() as connection:
                await connection.run_sync(metadata.drop_all, tables=tables)

    async def ping(self) -> bool:
        """Whether the database answers."""
        try:
            async with self.engine.connect() as connection:
                await connection.execute(sa.text("SELECT 1"))
        except sa.exc.SQLAlchemyError:
            return False
        return True

    async def dispose(self, *, close: bool = True) -> None:
        """Dispose of the engine and its connection pool."""
        with self._engine_lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            await engine.dispose(close=close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.dispose()


async def _owned(
    connection: AsyncConnection,
    scope: _Scope[AsyncConnection, AsyncSession],
) -> tuple[AsyncSession | None, AsyncTransaction | None]:
    """Return what a borrowing block ends: a session's transaction, or its own.

    A session that began one goes on using it, so that one ends through the
    session. Anything else is the block's to end.
    """
    session = scope.session
    if session is not None and session.in_transaction():
        return session, None
    return None, connection.get_transaction() or await connection.begin()


class Transaction(
    AsyncContextDecorator,
    AbstractAsyncContextManager["AsyncConnection"],
):
    """The transaction an awaited block runs, as an object.

    [`Database.transaction`][sqlakit.asyncio.Database.transaction] returns this.

    Kept as a class rather than a generator so that it also works as a
    decorator, and so that a single instance can be entered more than once,
    as ``@db.transaction()`` on a coroutine function does.
    """

    def __init__(
        self,
        db: Database,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
    ) -> None:
        self.db = db
        self.savepoint = savepoint
        self.join_nested = join_nested
        self.rollback = rollback
        self.commit_on_error = commit_on_error
        self._stacks: list[AsyncExitStack] = []

    def _recreate_cm(self) -> Transaction:
        """Give every decorated call a transaction of its own.

        `AsyncContextDecorator` reuses one instance for all calls, and two of
        them running at once would unwind each other's blocks.
        """
        return Transaction(
            self.db,
            savepoint=self.savepoint,
            join_nested=self.join_nested,
            rollback=self.rollback,
            commit_on_error=self.commit_on_error,
        )

    async def __aenter__(self) -> AsyncConnection:
        stack = AsyncExitStack()
        elsewhere = self.db._stand_in()  # noqa: SLF001
        if elsewhere is not None:
            connection = await stack.enter_async_context(
                elsewhere.transaction(
                    savepoint=self.savepoint,
                    join_nested=self.join_nested,
                    rollback=self.rollback,
                    commit_on_error=self.commit_on_error,
                )
            )
            self._stacks.append(stack)
            return connection
        try:
            outer, savepoint = self.db._plan(  # noqa: SLF001
                savepoint=self.savepoint,
                rollback=self.rollback,
            )
            owner: AsyncSession | None = None
            if outer is not None:
                connection = outer.connection
                holder = outer.savepoint_owner()
                # Without a savepoint the block only takes part in the
                # transaction around it, which commits it.
                if not savepoint:
                    transaction = None
                elif holder is not None:
                    # The session holding this connection's savepoints takes
                    # this one too, so a commit of its own releases them in the
                    # order they were taken.
                    transaction = await holder.begin_nested()
                    # It opens the savepoint when it next reaches the
                    # connection, which may be after this block has written
                    # through a session of its own.
                    await holder.connection()
                else:
                    transaction = await connection.begin_nested()
                held_by = outer.owner
            else:
                borrowed = self.db._scope_to_borrow()  # noqa: SLF001
                if borrowed is not None:
                    connection = await self.db._areused(borrowed)  # noqa: SLF001
                    owner, transaction = await _owned(connection, borrowed)
                else:
                    connection = await stack.enter_async_context(
                        self.db.engine.connect()
                    )
                    transaction = await connection.begin()
                held_by = None
            # Unwound in reverse: session, context, transaction, connection.
            stack.push_async_exit(self._finish(transaction, owner, connection))
            bound = stack.enter_context(
                self.db._set_outer(  # noqa: SLF001
                    connection,
                    join_nested=self.join_nested,
                    savepoint=savepoint,
                    owner=held_by,
                )
            )
            scope = stack.enter_context(self.db._bind(connection))  # noqa: SLF001
            if bound is not None:
                bound.scope = scope
                if self.db._owns_savepoints(outer, savepoint=savepoint):  # noqa: SLF001
                    bound.owner = scope
            stack.push_async_exit(self._close_session(scope))
        except BaseException:
            await stack.aclose()
            raise
        self._stacks.append(stack)
        return connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._stacks.pop().__aexit__(exc_type, exc, traceback)

    def _close_session(
        self,
        scope: _Scope[AsyncConnection, AsyncSession],
    ) -> Callable[..., Coroutine[None, None, None]]:
        """Commit the block's session, if it opened one, then close it.

        The block is the unit of work, so what its session holds belongs to the
        transaction; closing first would roll it back.
        """

        async def close_session(
            _exc_type: object,
            exc: BaseException | None,
            _traceback: object,
        ) -> None:
            if scope.session is None:
                return
            if self._keeps(exc):
                await scope.session.commit()
            await scope.session.close()

        return close_session

    def _finish(
        self,
        transaction: AsyncTransaction | None,
        owner: AsyncSession | None = None,
        connection: AsyncConnection | None = None,
    ) -> Callable[..., Coroutine[None, None, None]]:
        """Commit or roll back, unless this block only takes part in another."""

        async def finish(
            _exc_type: object,
            exc: BaseException | None,
            _traceback: object,
        ) -> None:
            if owner is not None:
                # The lending block's session holds the transaction. Ending it
                # through the session leaves that session able to go on.
                if self._keeps(exc) and not self.rollback:
                    await owner.commit()
                else:
                    await owner.rollback()
                return
            if transaction is None:
                return
            if not transaction.is_active:
                if connection is not None and connection.in_transaction():
                    # The session of an enclosing block ended this savepoint by
                    # committing, and the transaction around it holds the work.
                    return
                # Rolled back from inside the block. Say so, unless an
                # exception is already on its way out with the reason.
                if exc is None:
                    raise TransactionRolledBackError
                return
            if self._keeps(exc) and not self.rollback:
                await transaction.commit()
            else:
                await transaction.rollback()

        return finish

    def _keeps(self, exc: BaseException | None) -> bool:
        """Whether the block's work is kept rather than undone."""
        if exc is None:
            return True
        return self.commit_on_error is not None and isinstance(
            exc, self.commit_on_error
        )


class RetryingTransaction(BaseRetryingTransaction):
    """A transaction that runs its block again.

    [`Database.transaction`][sqlakit.asyncio.Database.transaction] returns
    this when it is given ``retry_on``.
    """

    def __call__(self, func: _FuncT) -> _FuncT:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            transaction = self.transaction()
            if transaction.db.in_transaction():
                # Only the block that owns the transaction can restart it: a
                # retry inside would keep the snapshot that caused the
                # conflict, and the outer transaction fails anyway.
                logger.debug(
                    "%s runs inside another transaction; retrying is up to "
                    "whoever opened it.",
                    getattr(func, "__qualname__", func),
                )
                async with transaction:
                    return await func(*args, **kwargs)
            for attempt in itertools.count():
                try:
                    async with self.transaction():
                        return await func(*args, **kwargs)
                except Exception as exc:
                    if not self._retry(exc, attempt=attempt):
                        raise
                    await asyncio.sleep(self.backoff(attempt))
            raise AssertionError  # pragma: no cover - the loop returns or raises

        return cast("_FuncT", wrapper)
