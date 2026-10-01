from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import cache
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Self,
    TypeVar,
    cast,
    overload,
)

import sqlalchemy as sa
import sqlalchemy.event
from typing_extensions import Unpack

from ._discovery import import_string
from ._recording import (
    KEEP,
    WIDE,
    Recording,
    Statement,
    caller_stack,
    check,
    require_expectation,
    resolved,
)
from ._routing import Router, as_router
from .exceptions import (
    DEFAULT_ALIAS,
    HIDDEN_BLOCK,
    REGISTERED_DEFAULT,
    AliasInUseError,
    ConflictingDatabaseUrlError,
    DatabaseAlreadyConfiguredError,
    DatabaseNotConfiguredError,
    DefaultAliasError,
    MissingConnectionError,
    MissingDatabaseUrlError,
    MissingDefaultDatabaseError,
    MissingSessionError,
    RetryNotSupportedError,
    UnknownDatabaseError,
    UnregisteredDatabaseError,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator, Sequence
    from os import PathLike

    from sqlalchemy.engine import Engine

    from .types import (
        DatabaseConfig,
        EngineArgs,
        SessionArgs,
        TemplatesLike,
        UrlParts,
    )

    RouterFunction = Callable[[type[Any]], str | None]

__all__ = [
    "DEFAULT_ALIAS",
    "DEFAULT_ENGINE_ARGS",
    "DEFAULT_SESSION_ARGS",
    "BaseDatabase",
    "BaseRetryingTransaction",
    "RetryOn",
]

DEFAULT_ENGINE_ARGS: EngineArgs = {
    # Catch connections dropped by the server, a proxy or a failover.
    "pool_pre_ping": True,
    # Reopen before the idle timeouts of MySQL, PgBouncer and cloud balancers.
    "pool_recycle": 1800,
}

DEFAULT_SESSION_ARGS: SessionArgs = {
    # Attributes stay readable after a commit. Under asyncio the lazy SELECT
    # that expiry would trigger fails with MissingGreenlet.
    "expire_on_commit": False,
}


RetryOn = (
    type[BaseException]
    | tuple[type[BaseException], ...]
    | Callable[[BaseException], bool]
)
"""Exception types, or a predicate over the exception."""

_random = random.SystemRandom()


def _elsewhere(frames: tuple[str, ...], skipped: tuple[str, ...]) -> bool:
    """Whether one of the files a recording leaves out ran this statement."""
    return bool(skipped) and bool(frames) and frames[0].startswith(skipped)


ConnectionT = TypeVar("ConnectionT")
SessionT = TypeVar("SessionT")
ValueT = TypeVar("ValueT")


class _Lazy(Generic[ConnectionT]):
    """A connection checkout that has not happened yet.

    A lazy ``session_factory()`` block binds one instead of a connection.
    ``get`` and ``aget`` check out once, and cache what they opened.
    """

    __slots__ = ("_alock", "_lock", "connection", "open")

    def __init__(self, open: Callable[[], Any]) -> None:  # noqa: A002
        self.open = open
        self.connection: ConnectionT | None = None
        self._lock = threading.Lock()
        self._alock: asyncio.Lock | None = None

    def get(self) -> ConnectionT:
        """Materialize the connection, once, and return it."""
        with self._lock:
            if self.connection is None:
                self.connection = self.open()
        return self.connection

    async def aget(self) -> ConnectionT:
        """Materialize the connection, once, awaited."""
        if self.connection is not None:
            return self.connection
        # `asyncio` because the async engine is asyncio-only, and on the cell
        # rather than the database, which outlives any one loop.
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            if self.connection is None:
                connection = self.open()
                if inspect.isawaitable(connection):
                    connection = await connection
                self.connection = connection
        return self.connection


@dataclass(slots=True)
class _Scope(Generic[ConnectionT, SessionT]):
    """The connection bound to a context, and the session opened on top of it.

    The context variable holds this object, not the session, so a session
    opened later, including in a task that copies the context, still
    belongs to the block that bound the connection.

    A lazy ``session_factory()`` block binds a ``checkout`` and no connection.
    The connection lands here once something uses it.
    """

    connection: ConnectionT | None
    session: SessionT | None = None
    checkout: _Lazy[ConnectionT] | None = None
    autocommit: bool = False
    """Whether the connection is in ``AUTOCOMMIT``, where no transaction runs."""

    hidden: bool = False
    """Whether `unbound()` hides this scope from the code running under it."""


@dataclass(slots=True)
class _Outer(Generic[ConnectionT]):
    """The connection of the innermost transaction bound to a context.

    Args:
        connection: What blocks below reuse, with ``join_nested``.
        join_nested: Whether blocks below reuse that connection.
        savepoint: Whether nested blocks run as savepoints.
        scope: The block's own scope, whose session the connection is bound to.
        owner: The scope whose session owns the savepoints of this connection.
            That session joins with a savepoint of its own, and a block below
            takes its savepoint through it, so the two are released in the
            order they were taken. One owner per connection: two of them
            release each other's savepoints out of order.

    """

    connection: ConnectionT
    join_nested: bool = True
    savepoint: bool = False
    scope: Any = None
    owner: Any = None

    def savepoint_owner(self) -> Any:  # noqa: ANN401 - a session of either API
        """Return the session that owns the savepoints here, if one does."""
        return None if self.owner is None else self.owner.session


class _Binding(Generic[ValueT]):
    """A value bound to a context variable for as long as a block is open.

    The generator `@contextmanager` builds costs more than the two calls it
    saves, and every block binds two of these.
    """

    __slots__ = ("_token", "_value", "_var")

    def __init__(self, var: ContextVar[Any], value: ValueT) -> None:
        self._var = var
        self._value = value

    def __enter__(self) -> ValueT:
        self._token = self._var.set(self._value)
        return self._value

    def __exit__(self, *_: object) -> None:
        self._var.reset(self._token)


class BaseDatabase(Generic[ConnectionT, SessionT]):
    """The sync and async databases share this: everything that is not IO.

    Binding to the context lives here. Opening and closing connections and
    sessions is left to the subclass.
    """

    _engine: Any = None  # narrowed by the subclass

    def __init__(
        self,
        url: str | sa.URL | None = None,
        engine_args: EngineArgs | None = None,
        session_args: SessionArgs | None = None,
        templates: TemplatesLike | None = None,
        alias: str | None = None,
        **parts: Unpack[UrlParts],
    ) -> None:
        """Build a database on ``url``, or on the parts to make one from.

        ```python
        Database("postgresql+psycopg://localhost/app")

        Database(
            drivername="postgresql+psycopg",
            host="localhost",
            database="app",
        )

        Database(DB_URL, templates="app/sql")  # where `sql` reads templates from
        Database(WAREHOUSE_URL, alias="warehouse")  # what a recording calls it
        ```

        ``alias`` is the name a recorded statement carries, for telling two
        databases apart in a log or on the debug server's page. A registry
        names the databases it holds after the aliases they are registered
        under, so it is worth setting on a database you keep yourself.

        Raises:
            MissingDatabaseUrlError: if given neither a ``url`` nor the parts to
                build one.
            ConflictingDatabaseUrlError: if given both. Either is an
                ``InvalidDatabaseConfigError``.

        """
        config = cast("DatabaseConfig", dict(parts))
        if url is not None:
            config["url"] = url
        self.url = sa.make_url(url_from_config(config))
        self.templates = templates
        self.engine_args = DEFAULT_ENGINE_ARGS | (engine_args or {})
        self.session_args = DEFAULT_SESSION_ARGS | (session_args or {})
        # Dropped so that reconfiguring does not keep sessions built from the
        # arguments of the previous configuration.
        self._sessionmaker = None
        self._engine_lock = threading.Lock()
        self._scope: ContextVar[_Scope[ConnectionT, SessionT]] = ContextVar(
            f"{type(self).__name__}.scope"
        )
        self._outer: ContextVar[_Outer[ConnectionT] | None] = ContextVar(
            f"{type(self).__name__}.outer"
        )
        self._recordings: ContextVar[tuple[Recording, ...]] = ContextVar(
            f"{type(self).__name__}.recordings", default=()
        )
        self._stacks: ContextVar[bool] = ContextVar(
            f"{type(self).__name__}.stacks", default=False
        )
        self._skipped: ContextVar[tuple[str, ...]] = ContextVar(
            f"{type(self).__name__}.skipped", default=()
        )
        self._listening = 0
        self._listened: Any = None
        self._listening_lock = threading.Lock()
        self._name = alias or DEFAULT_ALIAS

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.url.render_as_string()!r})"

    def _current_scope(self) -> _Scope[ConnectionT, SessionT]:
        """Return the scope bound to the current context.

        Raises:
            MissingConnectionError: if no block is open.

        """
        try:
            scope = self._scope.get()
        except LookupError:
            raise MissingConnectionError from None
        if scope.hidden:
            raise MissingConnectionError(
                HIDDEN_BLOCK.format(what="connection")
            ) from None
        return scope

    @property
    def session(self) -> SessionT:
        """The session bound to the current context.

        Opened on first use, on the current
        [`connection`][sqlakit.Database.connection], and closed when that block
        exits.

        Raises:
            MissingSessionError: if no connection is bound.

        """
        try:
            scope = self._scope.get()
        except LookupError:
            raise MissingSessionError from None
        if scope.hidden:
            raise MissingSessionError(HIDDEN_BLOCK.format(what="session")) from None
        if scope.session is None:
            if scope.connection is None and scope.checkout is not None:
                scope.session = self._lazy_session(scope.checkout)
            else:
                scope.session = self._create_session(
                    cast("ConnectionT", scope.connection)
                )
        return scope.session

    @contextmanager
    def recording(  # noqa: PLR0913 - what a recording may report to, one each
        self,
        label: str | None = None,
        *,
        logger: logging.Logger | None = None,
        echo: bool = False,
        stacks: bool = False,
        skip_queries_from: Sequence[str | PathLike[str]] = (),
        into: Recording | None = None,
        send_to: Callable[[Recording], object] | None = None,
    ) -> Iterator[Recording]:
        """Record the statements of this block, and what they add up to.

        ```python
        logger = logging.getLogger(__name__)

        with db.recording("GET /users", logger=logger) as record:
            build_report()

        record.count, record.duplicates, record.slowest
        ```

        ``logger`` writes a summary when the block ends, at a level the numbers
        choose. ``echo`` prints the statements instead, coloured where `rich` is
        installed. ``send_to`` is called with the recording, to hand it on:
        `sqlakit_debugserver.DebugServer` is one. ``stacks`` has every
        statement remember the frames that led to it, at the cost of a stack walk
        each time. ``skip_queries_from`` names the files whose statements are none of
        your business: what those run is not recorded at all, which leaves a test's
        report showing the code under test rather than the rows a factory of the tests
        wrote to set the scene.

        Blocks nest, each recording what runs inside it, and the listeners come off
        after. `with` is right on either side, awaited or not: it listens, it does
        not run anything.
        """
        recording = Recording(label=label) if into is None else into
        self._listen()
        recordings = self._recordings.set((*self._recordings.get(), recording))
        asked = self._stacks.set(stacks or self._stacks.get())
        skipped = self._skipped.set(
            (*self._skipped.get(), *resolved(skip_queries_from))
        )
        try:
            yield recording
        finally:
            self._skipped.reset(skipped)
            self._stacks.reset(asked)
            self._recordings.reset(recordings)
            self._silence()
            if logger is not None:
                recording.log(logger)
            if echo:
                recording.echo()
            if send_to is not None:
                send_to(recording)

    @contextmanager
    def assert_queries(
        self,
        count: int | None = None,
        *,
        at_most: int | None = None,
        duplicates: bool = True,
    ) -> Iterator[Recording]:
        """Assert what the block asks of this database.

        ```python
        with db.assert_queries(2):
            User.query.order_by("name").page(limit=10)

        with db.assert_queries(at_most=5):
            render(dashboard)

        with db.assert_queries(duplicates=False):
            render(users)  # the N+1 test, without a number
        ```

        The three checks stand alone or together. What fails prints the statements,
        numbered and timed, with the repeated ones pointing at each other.

        This watches one database. `sqlakit.testing.assert_queries` watches the
        importable registries instead, or whichever database it is given.

        Args:
            count: The statements the block is expected to run.
            at_most: A ceiling, for a number that would be brittle.
            duplicates: Whether a statement may run more than once.

        Raises:
            TypeError: if there is nothing to assert.

        """
        require_expectation(count, at_most, duplicates)
        with self.recording() as recording:
            yield recording
        check(recording, count=count, at_most=at_most, duplicates=duplicates)

    def _listen(self) -> None:
        with self._listening_lock:
            if self._listening == 0:
                engine = getattr(self.engine, "sync_engine", self.engine)
                sa.event.listen(engine, "before_cursor_execute", self._statement_began)
                sa.event.listen(engine, "after_cursor_execute", self._statement_ended)
                # Held, rather than looked up again: a block that disposes the
                # database gets a new engine, and the listeners are on the old.
                self._listened = engine
            self._listening += 1

    def _silence(self) -> None:
        with self._listening_lock:
            self._listening -= 1
            if self._listening == 0 and self._listened is not None:
                engine, self._listened = self._listened, None
                sa.event.remove(engine, "before_cursor_execute", self._statement_began)
                sa.event.remove(engine, "after_cursor_execute", self._statement_ended)

    def _statement_began(self, connection: Any, *_arguments: Any) -> None:  # noqa: ANN401
        connection.info.setdefault("sqlakit_started", []).append(time.perf_counter())

    def _statement_ended(
        self,
        connection: Any,  # noqa: ANN401
        _cursor: Any,  # noqa: ANN401
        statement: str,
        parameters: Any,  # noqa: ANN401
        _context: Any,  # noqa: ANN401
        _many: bool,  # noqa: FBT001
    ) -> None:
        starts = connection.info.get("sqlakit_started")
        if not starts:
            # Began before the listeners attached; no start time, not recorded.
            return
        started = starts.pop()
        recordings = self._recordings.get()
        if not recordings or statement.split(None, 1)[0].upper() in _CONTROL:
            return
        stacks = self._stacks.get()
        skipped = self._skipped.get()
        frames = caller_stack(keep=WIDE) if stacks or skipped else ()
        if _elsewhere(frames, skipped):
            # A row a factory of the tests wrote, not what the block is about.
            return
        record = Statement(
            sql=statement,
            parameters=parameters,
            duration=time.perf_counter() - started,
            database=self._name,
            dialect=connection.dialect.name,
            stack=frames[:KEEP] if stacks else (),
        )
        for recording in recordings:
            recording.statements.append(record)

    def in_transaction(self) -> bool:
        """Whether a transaction is bound to the current context.

        False under ``connect()`` and ``autocommit()``, which open none.
        """
        return self._outer.get(None) is not None

    @property
    def aliases(self) -> tuple[str, ...]:
        """The names this database goes by, which for one database is its own.

        A registry has one for each database it holds. Both carry this, so code
        that takes either does not have to ask which it was given.
        """
        return (self._name,)

    def __getitem__(self, alias: str) -> Any:  # noqa: ANN401
        """Return this database, under the name it carries.

        `Any` rather than `Self`, so a registry can narrow it to the databases
        it holds.

        Raises:
            UnknownDatabaseError: if the alias is another database's.

        """
        if alias == self._name:
            return self
        raise UnknownDatabaseError(alias, self.aliases)

    def __contains__(self, alias: str) -> bool:
        return alias == self._name

    def in_session(self) -> bool:
        """Whether a session is open in the current context.

        A block opens one when something first asks for [`session`][sqlakit.Database.session], so
        this is False in a block that has only run statements on the
        connection. Reading it opens nothing.
        """
        scope = self._scope.get(None)
        return scope is not None and scope.session is not None

    @contextmanager
    def unbound(self) -> Iterator[None]:
        """Hide the block open around this one, for the code inside to open its own.

        A test opens a transaction for the whole test, so code that reaches for
        `session` without a block of its own borrows one and passes, where in
        production it raises `MissingSessionError`. Wrap the call under test:

        ```python
        @pytest.mark.db
        def test_the_handler_opens_a_block() -> None:
            with db.unbound():
                handle(event)
        ```

        A block opened inside still joins the transaction around it, and rolls
        back with it.
        """
        scope = self._scope.get(None)
        if scope is None:
            yield
            return
        token = self._scope.set(replace(scope, session=None, hidden=True))
        try:
            yield
        finally:
            self._scope.reset(token)

    @property
    def engine(self) -> Any:  # noqa: ANN401
        """The engine underneath, which the subclass makes."""
        raise NotImplementedError  # pragma: no cover - the subclass has it

    def _create_session(self, connection: ConnectionT) -> SessionT:
        raise NotImplementedError  # pragma: no cover - the subclass has it

    def _lazy_session(self, cell: _Lazy[ConnectionT]) -> SessionT:
        raise NotImplementedError  # pragma: no cover - the subclass has it

    def _session_args_for(self, connection: ConnectionT) -> dict[str, Any]:
        """How a session joins the transaction already open on ``connection``."""
        outer = self._outer.get(None)
        if outer is None or outer.connection is not connection:
            return {}
        if outer.owner is not None and outer.owner is self._scope.get(None):
            return {"join_transaction_mode": "create_savepoint"}
        # Spelled out: SQLAlchemy's default would open a savepoint of its own
        # whenever the connection is already inside one.
        return {"join_transaction_mode": "rollback_only"}

    @staticmethod
    def _owns_savepoints(outer: _Outer[Any] | None, *, savepoint: bool) -> bool:
        """Whether this block's session owns the savepoints of its connection.

        A block rolled back at the end gives its session one, so a test or the
        code under it may commit that session without ending the block. A block
        that took a savepoint of its own does not: the owner stays the one
        above, and blocks below take their savepoints through it.
        """
        if outer is None:
            return savepoint
        return not savepoint and outer.owner is not None

    def _plan(self, *, savepoint: bool, rollback: bool) -> tuple[_Outer | None, bool]:
        """Decide what a transaction joins, and whether it is a savepoint.

        A block to be rolled back needs a savepoint to roll back to, and a
        block inside an isolated one stays isolated.
        """
        outer = self._outer_to_join()
        return outer, savepoint or rollback or bool(outer and outer.savepoint)

    def _stand_in(self) -> Any:  # noqa: ANN401
        """Return the database whose blocks this one opens instead, or None.

        A database opens its own. A registry opens those of the database it
        holds or is overridden with, looked up as each block opens, so a block
        decorated at import follows an override.
        """
        return None

    def _outer_to_join(self) -> _Outer[ConnectionT] | None:
        """Return the outer transaction new blocks join, if there is one."""
        outer = self._outer.get(None)
        return outer if outer is not None and outer.join_nested else None

    def _connection_to_join(self) -> ConnectionT | None:
        outer = self._outer_to_join()
        return outer.connection if outer is not None else None

    def _scope_to_reuse(self) -> _Scope[ConnectionT, SessionT] | None:
        """Return the bound scope a new block reuses, if it may.

        One connection per context: a block that only needs a connection takes
        the one already bound, whether a transaction, ``autocommit()`` or
        another ``connect()`` opened it. ``join_nested=False`` opts out. The
        caller materializes a lazy scope, awaited or not.
        """
        outer = self._outer.get(None)
        if outer is not None and not outer.join_nested:
            return None
        return self._scope.get(None)

    def _scope_to_borrow(self) -> _Scope[ConnectionT, SessionT] | None:
        """Return the scope whose connection a transaction runs on, if it may.

        ``connect()`` and ``session_factory()`` lend theirs. ``autocommit()``
        does not: no transaction runs on an ``AUTOCOMMIT`` connection.
        """
        scope = self._scope_to_reuse()
        if scope is None or scope.autocommit:
            return None
        return scope

    def _bind(
        self,
        connection: ConnectionT | None,
        checkout: _Lazy[ConnectionT] | None = None,
        *,
        autocommit: bool = False,
    ) -> _Binding[_Scope[ConnectionT, SessionT]]:
        """Bind a scope holding ``connection`` to the current context.

        Every block gets a scope, and so a session, of its own. Ending that
        session is left to the caller, which knows whether it takes an
        ``await``. A lazy block passes ``checkout`` instead of a connection.
        """
        # Unparameterized: subscripting a generic builds an alias and calls
        # through it, and every block binds a scope.
        scope: _Scope[ConnectionT, SessionT] = _Scope(
            connection, checkout=checkout, autocommit=autocommit
        )
        return _Binding(self._scope, scope)

    def _set_outer(
        self,
        connection: ConnectionT | None,
        *,
        join_nested: bool = True,
        savepoint: bool = False,
        owner: Any = None,  # noqa: ANN401 - the scope whose session owns them
    ) -> _Binding[_Outer[ConnectionT] | None]:
        """Make ``connection`` the outer one for this context. See `_Outer`.

        ``None`` leaves this context without an outer transaction at all, as
        ``autocommit()`` needs: blocks under it must not join a transaction
        its own connection is not part of.
        """
        outer = (
            _Outer(
                connection,
                join_nested=join_nested,
                savepoint=savepoint,
                owner=owner,
            )
            if connection is not None
            else None
        )
        return _Binding(self._outer, outer)


DatabaseT = TypeVar("DatabaseT", bound="BaseDatabase[Any, Any]")


class _Using:
    """A database standing in for the default one, while a block of it is open.

    `db.using(alias)` returns one of these. Everything a database does, it
    does, and it adds the redirection: for as long as one of its blocks is
    open, a model that lives on the default database resolves here instead.
    """

    def __init__(
        self,
        db: Any,  # noqa: ANN401
        override: ContextVar[str | None],
        alias: str,
    ) -> None:
        self._db = db
        self._override = override
        self._alias = alias

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._alias!r}, {self._db!r})"

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Everything else is the database's own."""
        return getattr(self._db, name)

    def __enter__(self) -> Any:  # noqa: ANN401
        """Redirect for the block, opening nothing."""
        self._token = self._override.set(self._alias)
        return self._db

    def __exit__(self, *exc_info: object) -> None:
        self._override.reset(self._token)

    def connect(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open a connection here, and redirect while it is open."""
        return self._redirect(self._db.connect(**arguments))

    def transaction(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open a transaction here, and redirect while it is open."""
        return self._redirect(self._db.transaction(**arguments))

    def autocommit(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open an autocommit block here, and redirect while it is open."""
        return self._redirect(self._db.autocommit(**arguments))

    def session_factory(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open a session here, and redirect while it is open."""
        return self._redirect(self._db.session_factory(**arguments))

    def _redirect(self, block: Any) -> _Redirected:  # noqa: ANN401
        return _Redirected(block, self._override, self._alias)


class _Redirected:
    """A block of another database, with the redirection around it.

    Awaited or not, whichever the block underneath is.
    """

    def __init__(
        self,
        block: Any,  # noqa: ANN401
        override: ContextVar[str | None],
        alias: str,
    ) -> None:
        self._block = block
        self._override = override
        self._alias = alias

    def __enter__(self) -> Any:  # noqa: ANN401
        self._token = self._override.set(self._alias)
        try:
            return self._block.__enter__()
        except BaseException:
            self._override.reset(self._token)
            raise

    def __exit__(self, *exc_info: object) -> Any:  # noqa: ANN401
        try:
            return self._block.__exit__(*exc_info)
        finally:
            self._override.reset(self._token)

    async def __aenter__(self) -> Any:  # noqa: ANN401
        self._token = self._override.set(self._alias)
        try:
            return await self._block.__aenter__()
        except BaseException:
            self._override.reset(self._token)
            raise

    async def __aexit__(self, *exc_info: object) -> Any:  # noqa: ANN401
        try:
            return await self._block.__aexit__(*exc_info)
        finally:
            self._override.reset(self._token)


class _DatabaseRegistryMixin(BaseDatabase[Any, Any], Generic[DatabaseT]):
    """The registry half of the database an application imports.

    Mixed into the concrete `Databases` on either side, which adds the database
    half and the disposal the asyncio one awaits.
    """

    _database_class: type[DatabaseT]

    def __init__(self) -> None:
        """Leave everything to [`configure`][sqlakit.Databases.configure]."""
        self._default: DatabaseT | None = None
        self._aliased: dict[str, DatabaseT] = {}
        self._overridden: dict[str, DatabaseT] = {}
        self._routers: tuple[Any, ...] = ()
        self._using: ContextVar[str | None] = ContextVar(
            f"{type(self).__name__}.using", default=None
        )

    def __repr__(self) -> str:
        if self._built_its_own:
            return super().__repr__()
        held = self._stand_in()
        if held is None:
            return f"{type(self).__name__}(unconfigured)"
        return f"{type(self).__name__}({held!r})"

    def __getitem__(self, alias: str) -> Self | DatabaseT:
        """Return the database configured as ``alias``.

        ``db["default"]`` is the database the code reaches without an alias: this
        registry, or the database `register` was given for that name.

        Raises:
            UnknownDatabaseError: if nothing is configured under that alias.

        """
        if alias in self._overridden:
            return self._overridden[alias]
        if alias == DEFAULT_ALIAS:
            return self if self._default is None else self._default
        try:
            return self._aliased[alias]
        except KeyError:
            raise UnknownDatabaseError(alias, self.aliases) from None

    def __contains__(self, alias: str) -> bool:
        return (
            alias == DEFAULT_ALIAS
            or alias in self._aliased
            or alias in self._overridden
        )

    def register(self, alias: str, db: DatabaseT) -> None:
        """Put a database already built under an alias.

        `configure` takes settings and builds the databases. This takes one you
        built yourself, for a shard that only exists once the application is
        running, or for a registry that never reads settings at all:

        ```python
        db.register("shard-7", Database(SHARD_URL))
        ```

        `default` is the alias the code reaches without naming one, and a
        database registered under it stands where `configure` would have built
        one. The registry itself is then a registry alone: reach that database
        as `db["default"]`, or through the models that live on it.

        The alias has to be free. Replacing one under a name already in use
        would leave the code that holds the old database talking to it.

        Raises:
            AliasInUseError: if another database holds that alias.
            DefaultAliasError: if `default` is asked for and the registry
                already has one.

        """
        if alias == DEFAULT_ALIAS:
            if self.is_configured:
                raise DefaultAliasError
            self._default = self._named(alias, db)
            return
        if alias in self._aliased:
            raise AliasInUseError(alias)
        self._aliased[alias] = self._named(alias, db)

    @contextmanager
    def recording(  # noqa: PLR0913 - what a recording may report to, one each
        self,
        label: str | None = None,
        *,
        logger: logging.Logger | None = None,
        echo: bool = False,
        stacks: bool = False,
        skip_queries_from: Sequence[str | PathLike[str]] = (),
        into: Recording | None = None,
        send_to: Callable[[Recording], object] | None = None,
        using: str | DatabaseT | Sequence[str | DatabaseT] | None = None,
    ) -> Iterator[Recording]:
        """Record every database this registry has, not the default one alone.

        ```python
        with db.recording() as record:
            move_the_reports()

        record.databases  # ("default", "warehouse")
        ```

        Statements say which database ran them. ``using`` narrows the block to
        the databases named, by alias or in person, as `assert_queries` takes
        them:

        ```python
        with db.recording(using="warehouse"):
            move_the_reports()

        with db.recording(using=["default", "warehouse"]):
            move_the_reports()
        ```

        `db["warehouse"].recording()` records that one on its own.

        Raises:
            UnknownDatabaseError: if ``using`` names an alias nothing holds.

        """
        together = Recording(label=label) if into is None else into
        databases = self._recorded(using)
        with ExitStack() as stack:
            for db in databases:
                stack.enter_context(
                    BaseDatabase.recording(
                        db,
                        label,
                        stacks=stacks,
                        skip_queries_from=skip_queries_from,
                        into=together,
                    )
                )
            try:
                yield together
            finally:
                if logger is not None:
                    together.log(logger)
                if echo:
                    together.echo()
                if send_to is not None:
                    send_to(together)

    @contextmanager
    def unbound(self) -> Iterator[None]:
        """Hide the block open on every database this registry holds."""
        with ExitStack() as stack:
            for alias in self.aliases:
                stack.enter_context(BaseDatabase.unbound(self[alias]))
            yield

    def _recorded(
        self, using: str | DatabaseT | Sequence[str | DatabaseT] | None
    ) -> tuple[Any, ...]:
        """Return the databases a recording watches: the ones named, or all of them."""
        if using is None:
            return tuple(self[alias] for alias in self.aliases)
        asked = using if isinstance(using, (list, tuple, set, frozenset)) else (using,)
        return tuple(self[one] if isinstance(one, str) else one for one in asked)

    @staticmethod
    def _named(alias: str, db: DatabaseT) -> DatabaseT:
        """Let a database say which alias it answers to, when it is recorded."""
        db._name = alias  # noqa: SLF001
        return db

    @contextmanager
    def override(
        self,
        db: DatabaseT,
        *,
        alias: str = DEFAULT_ALIAS,
    ) -> Iterator[DatabaseT]:
        """Put another database under an alias for the block, and return it.

        For a test that runs the application against a database of its own:

        ```python
        with Database(TEST_URL) as test_db, db.override(test_db):
            ...
        ```

        Everything that reaches the alias through this registry reaches that
        database: `db.session` and `db.transaction()` for the default one,
        `db["replica"]` for another, the models that live on it, and
        `@transaction`. It holds for the whole process, every thread and task,
        as the override of a dependency-injection container does, so a server
        the test drives in another thread sees it too.

        The alias does not have to be configured, and the database does not have
        to be registered. On exit the alias means what it meant before, and the
        database is left open: whoever built it disposes of it.

        `using` is the other way to send a model elsewhere. It keeps every alias
        as it is and sends the models on the default one to another alias,
        where this changes what the alias is.
        """
        before = self._overridden.get(alias)
        named = getattr(db, "_name", None)
        self._overridden[alias] = self._named(alias, db)
        try:
            yield db
        finally:
            if before is None:
                del self._overridden[alias]
            else:
                self._overridden[alias] = before
            if named is not None:
                db._name = named  # noqa: SLF001

    def _stand_in(self) -> DatabaseT | None:
        """Return the database the default alias proxies to, if not this registry."""
        held = self._overridden.get(DEFAULT_ALIAS, self._default)
        return None if held is self else held

    def using(self, target: str | DatabaseT) -> _Using:
        """Return that database, standing in for the default one.

        Named or handed over, as `recording(using=...)` and a query's `using()`
        take it.

        The block opens on it, and models that live on the default database resolve
        there for as long as it is open:

        ```python
        with db.using("replica").connect():
            report = build_report()
        ```

        Models that live somewhere else stay where they are, and a query that named
        its own database with `using()` still wins. Entered on its own, as `with
        db.using("replica"):`, it redirects and opens nothing.

        Raises:
            UnknownDatabaseError: if nothing is configured under that alias.
            UnregisteredDatabaseError: if the database is not one this registry
                holds, since the redirection works by the name it holds it under.

        """
        alias = target if isinstance(target, str) else self._alias_of(target)
        if alias not in self:
            raise UnknownDatabaseError(alias, self.aliases)
        return _Using(self[alias], self._using, alias)

    def _alias_of(self, db: DatabaseT) -> str:
        """Return the alias this registry holds a database under.

        Raises:
            UnregisteredDatabaseError: if it holds it under none.

        """
        alias = self._held_as(db)
        if alias is None:
            raise UnregisteredDatabaseError(self.aliases)
        return alias

    def _held_as(self, db: object) -> str | None:
        """Return the alias this registry holds a database under, or None."""
        for alias, held in self._overridden.items():
            if held is db:
                return alias
        # A configured registry is the default database itself, and a
        # registered one holds it.
        if db is self or db is self._default:
            return DEFAULT_ALIAS
        for alias, held in self._aliased.items():
            if held is db:
                return alias
        return None

    def route(self, *routers: Router | RouterFunction | str) -> None:
        """Say which database a model lives on, for models that do not say it.

        Each router takes a model and returns an alias, or None to leave the question
        to the next one. A dotted path is imported, since settings carry paths:

        ```python
        db.route(lambda model: "warehouse" if is_report(model) else None)
        db.route("app.db.routing")
        ```

        Placement is structural: reads, writes and the tables `provisioned_tables()`
        creates all follow it. Called with nothing, it clears the policy, which
        leaves `__db__` on a model as the only answer.
        """
        self._routers = tuple(
            as_router(import_string(router) if isinstance(router, str) else router)
            for router in routers
        )

    @property
    def routers(self) -> tuple[Any, ...]:
        """The placement policy in force, in the order it is asked."""
        return self._routers

    def db_for(self, model: type[Any]) -> BaseDatabase[Any, Any]:
        """Return the database a model lives on.

        The routers first, then the model's own ``__db__``, unless a block
        opened with `using()` stands in for the default database.
        """
        placement = self._routed(model) or model.__db__
        if not isinstance(placement, str):
            # A model pinned to a database this registry holds follows the
            # alias it is held under, `using()` and `override()` alike.
            alias = self._held_as(placement)
            if alias is None:
                return placement
            placement = alias
        redirected = self._using.get()
        if redirected is not None and placement == DEFAULT_ALIAS:
            placement = redirected
        return self[placement]

    def _routed(self, model: type[Any]) -> str | None:
        """Return what the first router says about this model, if anything."""
        for router in self._routers:
            alias = router(model)
            if alias is not None:
                return alias
        return None

    @property
    def aliases(self) -> tuple[str, ...]:
        """The aliases configured or overridden, the default one first."""
        overridden = (
            alias
            for alias in self._overridden
            if alias != DEFAULT_ALIAS and alias not in self._aliased
        )
        return (DEFAULT_ALIAS, *self._aliased, *overridden)

    @property
    def is_configured(self) -> bool:
        """Whether this registry has a default database to reach."""
        return "url" in self.__dict__ or self._stand_in() is not None

    @property
    def _built_its_own(self) -> bool:
        """Whether the default database is this registry, `configure` having built it."""
        return "url" in self.__dict__

    if not TYPE_CHECKING:
        # Hidden from type checkers, which keep reading these off `Database`
        # and its asyncio twin, signatures and all.
        def _proxy(name: str, *, attribute: bool = False) -> Any:  # noqa: ANN401, N805
            """Proxy to the database this registry holds, or call its own.

            A registry handed a default, or overridden for a block, proxies to
            that database. One that `configure` built calls what it inherits,
            which `super()` reaches.
            """

            def reach(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                held = self._stand_in()
                found = (
                    getattr(held, name) if held is not None else getattr(super(), name)
                )
                return found if attribute else found(*args, **kwargs)

            return property(reach) if attribute else reach

        connection = _proxy("connection", attribute=True)
        engine = _proxy("engine", attribute=True)
        session = _proxy("session", attribute=True)
        sql = _proxy("sql", attribute=True)
        assert_queries = _proxy("assert_queries")
        in_session = _proxy("in_session")
        in_transaction = _proxy("in_transaction")
        ping = _proxy("ping")
        provisioned_tables = _proxy("provisioned_tables")
        query = _proxy("query")
        del _proxy
        # `connect`, `autocommit`, `session_factory` and `transaction` are not
        # proxied: the ones inherited look `_stand_in` up as the block opens.

    @overload
    def configure(
        self,
        url: str | sa.URL | None = None,
        engine_args: EngineArgs | None = None,
        session_args: SessionArgs | None = None,
        routers: Sequence[Router | RouterFunction | str] = (),
        templates: TemplatesLike | None = None,
        **parts: Unpack[UrlParts],
    ) -> None: ...

    @overload
    def configure(
        self,
        url: Mapping[str, DatabaseConfig],
        *,
        routers: Sequence[Router | RouterFunction | str] = (),
        templates: TemplatesLike | None = None,
    ) -> None: ...

    def configure(
        self,
        url: str | sa.URL | Mapping[str, DatabaseConfig] | None = None,
        engine_args: EngineArgs | None = None,
        session_args: SessionArgs | None = None,
        routers: Sequence[Router | RouterFunction | str] = (),
        templates: TemplatesLike | None = None,
        **parts: Unpack[UrlParts],
    ) -> None:
        """Point this database at ``url``, or at several keyed by alias.

        Call it once, at startup. Settings arrive as a URL or as the parts to build
        one from:

        ```python
        db.configure(DB_URL)

        db.configure(
            drivername="postgresql+psycopg",
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
        )
        ```

        A mapping configures this database as its ``"default"`` and builds the rest
        alongside it, each with its own pool, connection and transactions:

        ```python
        db.configure(
            {
                "default": {"url": PRIMARY_URL, "engine_args": {"pool_size": 20}},
                "replica": {"url": REPLICA_URL},
            }
        )

        db["replica"].session  # the replica; `db.session` is the default one
        ```

        ``routers`` says where a model lives when the model does not, as `route`
        takes them. ``templates`` is where the SQL templates of every one of them
        live. Reconfiguring is allowed until something connects; afterwards, dispose
        of the engines first.

        Raises:
            DatabaseAlreadyConfiguredError: if a database has already connected.
            MissingDefaultDatabaseError: if a mapping carries no ``"default"``.
            MissingDatabaseUrlError: if an entry says nowhere to connect.
            ConflictingDatabaseUrlError: if one says it twice over. Either is an
                ``InvalidDatabaseConfigError``.

        """
        if self._default is not None:
            raise DefaultAliasError
        if not isinstance(url, Mapping):
            self._reject_if_connected()
            super().__init__(url, engine_args, session_args, templates, **parts)
            self.route(*routers)
            return
        if DEFAULT_ALIAS not in url:
            raise MissingDefaultDatabaseError(tuple(url))
        self._reject_if_connected()
        configs = {
            alias: (url_from_config(config), config) for alias, config in url.items()
        }
        default_url, default = configs[DEFAULT_ALIAS]
        super().__init__(
            default_url,
            default.get("engine_args"),
            default.get("session_args"),
            templates,
        )
        self._aliased = {
            alias: self._named(
                alias,
                self._database_class(
                    database_url,
                    config.get("engine_args"),
                    config.get("session_args"),
                    templates,
                ),
            )
            for alias, (database_url, config) in configs.items()
            if alias != DEFAULT_ALIAS
        }
        self.route(*routers)

    def _reject_if_connected(self) -> None:
        connected = self._built_its_own and self._engine is not None
        if connected or any(
            db._engine is not None  # noqa: SLF001
            for db in self._aliased.values()
        ):
            raise DatabaseAlreadyConfiguredError

    if not TYPE_CHECKING:
        # Hidden from type checkers: seeing it, they would take every attribute
        # to exist and stop reporting typos. It is reached when normal lookup
        # fails, as it does on a registry with no database of its own, from
        # the outside and from its own methods.
        def __getattr__(self, name: str) -> object:
            # A dunder is the machinery asking, and a container that patched
            # `__getattribute__` bounces back here until it is answered.
            if name.startswith("__") and name.endswith("__"):
                raise AttributeError(name)
            # Only the database half is worth explaining. Anything else is a
            # name that does not exist, and saying so lets `hasattr`,
            # `copy` and every library that introspects work.
            # Through `object`, so a patched `__getattribute__` cannot loop.
            state = object.__getattribute__(self, "__dict__")
            if "url" in state:
                raise AttributeError(name)
            asked_as_a_database = name in DATABASE_STATE or (
                not name.startswith("_") and hasattr(type(self), name)
            )
            if not asked_as_a_database:
                raise AttributeError(name)
            if state.get("_default") is not None:
                raise DatabaseNotConfiguredError(REGISTERED_DEFAULT) from None
            raise DatabaseNotConfiguredError from None


DATABASE_STATE = frozenset(
    # The attributes `BaseDatabase.__init__` sets. A registry has these once
    # it has a database of its own, and reaching for one before then raises
    # `DatabaseNotConfiguredError`, whichever method asked.
    {
        "url",
        "templates",
        "engine_args",
        "session_args",
        "_sessionmaker",
        "_engine_lock",
        "_scope",
        "_outer",
        "_recordings",
        "_stacks",
        "_skipped",
        "_listening",
        "_listened",
        "_listening_lock",
        "_name",
    }
)

_CONTROL = frozenset(
    # Transaction control is not a query, and which of these reach a cursor
    # depends on the driver, and counting them would make a recording mean
    # something different on SQLite than on PostgreSQL.
    {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA"}
)

URL_PARTS = (
    "drivername",
    "username",
    "password",
    "host",
    "port",
    "database",
    "query",
)
"""The parts a configuration is spelled with instead of a ``url``."""


def url_from_config(config: DatabaseConfig) -> str | sa.URL:
    """Read the URL out of a configuration, however it was spelled.

    Raises:
        MissingDatabaseUrlError: if given neither a ``url`` nor the parts to build
            one.
        ConflictingDatabaseUrlError: if given both.

    """
    parts = {part: config[part] for part in URL_PARTS if part in config}
    if "url" in config:
        if parts:
            raise ConflictingDatabaseUrlError(tuple(parts))
        return config["url"]
    if "drivername" not in parts:
        raise MissingDatabaseUrlError
    return sa.URL.create(**parts)  # ty: ignore[invalid-argument-type]


class _LazyBind:
    """A session that checks its connection out on first real use.

    Mixed over the session class of a lazy ``session_factory()`` block, which
    creates it unbound. ``get_bind`` checks the connection out on a flush or a
    query, not on ``add()``.
    """

    _sqlakit_checkout: Callable[[], Any] | None = None
    bind: Any

    def get_bind(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if self.bind is None and self._sqlakit_checkout is not None:
            self.bind = self._sqlakit_checkout()
        return super().get_bind(*args, **kwargs)  # ty: ignore[unresolved-attribute]


@cache
def lazy_session_class(base: type) -> type:
    """Return ``base`` with `_LazyBind` mixed in, built once per base."""
    if issubclass(base, _LazyBind):
        return base
    return type(f"Lazy{base.__name__}", (_LazyBind, base), {})


class BaseRetryingTransaction:
    """``transaction()`` returns this when given ``retry_on``.

    A decorator, and deliberately not a context manager: retrying re-runs the
    block, which a ``with`` statement cannot do. Entering one is rejected by
    type checkers and raises at runtime.
    """

    def __init__(
        self,
        transaction: Callable[[], Any],
        *,
        retry_on: RetryOn,
        max_retries: int = 3,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        self.transaction = transaction
        self.retry_on = retry_on
        self.max_retries = max_retries
        self.backoff = backoff or default_backoff

    def _retry(self, exc: BaseException, *, attempt: int) -> bool:
        return attempt < self.max_retries and retry_matches(exc, self.retry_on)

    if not TYPE_CHECKING:
        # Hidden from type checkers, which reject `with` on this class outright.
        # Defined for the error message.
        def __enter__(self) -> None:
            raise RetryNotSupportedError

        def __exit__(self, *exc_info: object) -> None:
            raise AssertionError  # pragma: no cover

        async def __aenter__(self) -> None:
            raise RetryNotSupportedError

        async def __aexit__(self, *exc_info: object) -> None:
            raise AssertionError  # pragma: no cover


def late_bound(source: object, registry: Any) -> Any:  # noqa: ANN401
    """Return the database a late-bound decorator opens its block on, now.

    Nothing is the registry, whose default is looked up as the block opens. A
    name is an alias in it, a callable is asked, and anything else is the
    database itself.
    """
    if source is None:
        return registry
    if isinstance(source, str):
        return registry[source]
    if callable(source):
        return source()
    return source


def retry_matches(exc: BaseException, retry_on: RetryOn) -> bool:
    """Whether ``retry_on`` claims this exception is worth another attempt."""
    if isinstance(retry_on, type | tuple):
        return isinstance(exc, retry_on)
    return retry_on(exc)


def default_backoff(attempt: int) -> float:
    """Exponential backoff with jitter: ~0.1s, ~0.2s, ~0.4s, ..."""
    return 0.1 * (2**attempt) * (0.5 + _random.random())


def fix_sqlite_transactions(engine: Engine) -> None:
    """Make the stdlib SQLite driver emit real transactions.

    ``sqlite3`` never emits ``BEGIN`` on its own, which leaves ``SAVEPOINT``
    and nested transactions broken. The workaround is SQLAlchemy's, and covers
    ``pysqlite`` and ``aiosqlite`` alike.
    """
    if engine.dialect.name != "sqlite":
        return

    @sa.event.listens_for(engine, "connect")
    def disable_implicit_begin(dbapi_connection: Any, _record: object) -> None:  # noqa: ANN401
        dbapi_connection.isolation_level = None

    @sa.event.listens_for(engine, "begin")
    def emit_begin(connection: sa.Connection) -> None:
        # SQLAlchemy signals a begin under AUTOCOMMIT too, where a real
        # transaction would undo what AUTOCOMMIT was asked for.
        if _is_autocommit(connection):
            return
        connection.exec_driver_sql("BEGIN")


def _is_autocommit(connection: sa.Connection) -> bool:
    """Whether ``connection`` runs in ``AUTOCOMMIT``, per block or per engine."""
    if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        return True
    return getattr(connection.dialect, "_on_connect_isolation_level", None) == (
        "AUTOCOMMIT"
    )
