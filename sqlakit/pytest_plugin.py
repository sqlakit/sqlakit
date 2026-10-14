"""The `db` marker, and the fixtures behind it.

Installed with the library, so a project says which models it has and nothing
else:

```python title="conftest.py"
import pytest

from app.models import Model


@pytest.fixture(scope="session")
def sqlakit_base() -> type[Model]:
    return Model
```

The database comes from the base: the registry its models look their aliases
up in, or the one they were given in person. A project with no model layer
defines `sqlakit_db` and `sqlakit_metadata` instead.

A test marked `db` runs in a transaction that rolls back, on every database.
`using` narrows that to the ones a test works on, and `unbound` hides the
test's own block from the code it calls, as production has none:

```python
@pytest.mark.db(using="warehouse")
@pytest.mark.db(using=["default", "warehouse"])
@pytest.mark.db(unbound=True)
```

`sqlakit_unbound = true` says the last one for the whole suite, and
`db(unbound=False)` gives one test its block back.

Every other test connects to nothing, and reaching for a session there raises
`MissingSessionError` rather than opening one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import pathlib
import time
import warnings
from collections.abc import Mapping
from contextlib import AsyncExitStack, ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from ._discovery import import_models
from ._recording import Recording, check, require_expectation
from ._registry import db as importable_db
from .exceptions import UnknownDatabaseError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator, Sequence

    import sqlalchemy as sa

    from .types import AssertQueries

MARKER = "db"
SYNC_FIXTURE = "_sqlakit_transaction"
ASYNC_FIXTURE = "_sqlakit_async_transaction"
REPORT = pytest.StashKey[list[tuple[Recording, str, list[str]]]]()
WHERE = pytest.StashKey["pathlib.Path | None"]()
NAMED_BY_THE_CLOCK = ""
"""What `--sqlakit-report` with no path means."""


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "sqlakit",
        "give the tests marked `db` a database, and the rest none",
        type="bool",
        default=False,
    )
    parser.addini(
        "sqlakit_unbound",
        "hide the test's own block, so the code it calls opens one as in production",
        type="bool",
        default=False,
    )
    parser.addini(
        "sqlakit_models",
        "import every `models` module under these packages before creating tables",
        type="linelist",
        default=[],
    )
    parser.addini(
        "sqlakit_skip_queries_from",
        "the queries these files run stay out of the report: a factory, a helper",
        type="paths",
        default=[],
    )
    parser.addoption(
        "--sqlakit-report",
        metavar="PATH",
        nargs="?",
        const=NAMED_BY_THE_CLOCK,
        default=None,
        help=(
            "write a page of every statement the marked tests ran; "
            "on its own the name carries the time the run finished"
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getini("sqlakit"):
        config.addinivalue_line("markers", f"{MARKER}: the test needs a database")
    config.stash[REPORT] = []
    config.stash[WHERE] = _report_path(config)  # named now, so a bad path fails now


def pytest_terminal_summary(
    terminalreporter: Any,  # noqa: ANN401 - the reporter of whichever pytest runs
    config: pytest.Config,
) -> None:
    """Write the report at the end, and say where it went."""
    where = config.stash.get(WHERE, None)
    reported = config.stash.get(REPORT, [])
    if where is None or not reported:
        return
    # Installed apart from `sqlakit`, and only when a report is asked for.
    from sqlakit_debugserver import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        as_payload,
        write_report,
    )

    records = [
        as_payload(recording, app=app, tags=tags) for recording, app, tags in reported
    ]
    tests = len({record["app"] + record["label"] for record in records})
    queries = sum(record["count"] for record in records)
    written = write_report(where, records, about=f"{tests} tests · {queries} queries")
    terminalreporter.write_line(f"sqlakit wrote {written}")


def _report_path(config: pytest.Config) -> pathlib.Path | None:
    """Return where the report goes, if one was asked for.

    ``--sqlakit-report`` on its own names the file after the clock, so a run
    keeps the one before it.
    """
    asked = config.getoption("--sqlakit-report", default=None)
    if asked is None:
        return None
    if asked == NAMED_BY_THE_CLOCK:
        where = config.rootpath / time.strftime("sqlakit-%Y%m%d-%H%M%S.html")
    elif (where := pathlib.Path(asked)).is_dir():
        # `--sqlakit-report tests/unit` reads as the path to write to.
        message = (
            f"--sqlakit-report was given `{asked}`, which is a directory. "
            f"Write `--sqlakit-report=PATH` for a file, or the flag on its own "
            f"for a name with the time in it."
        )
        raise pytest.UsageError(message)
    if importlib.util.find_spec("sqlakit_debugserver") is None:
        message = (
            "--sqlakit-report writes the debug server's page, which comes with "
            "it: pip install sqlakit-debugserver"
        )
        raise pytest.UsageError(message)
    return where


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Give a database to the marked tests and to nothing else.

    The fixture is added here rather than with `usefixtures` because an async
    one cannot be fetched from a synchronous fixture, and `pytest` reads
    `fixturenames` at this moment.
    """
    if not config.getini("sqlakit"):
        return
    for item in items:
        if not isinstance(item, pytest.Function):
            continue
        if item.get_closest_marker(MARKER) is None:
            continue
        awaited = inspect.iscoroutinefunction(item.function)
        name = ASYNC_FIXTURE if awaited else SYNC_FIXTURE
        item.fixturenames.insert(_before_the_test_s_own(item), name)


def _before_the_test_s_own(item: pytest.Function) -> int:
    """Return where the transaction goes among the fixtures of a test.

    `fixturenames` is the order they set up in. A fixture of the test's own
    that writes has to run inside the transaction, and one of a wider scope,
    the schema or a module that seeds itself, has to run outside it.
    """
    known = item._fixtureinfo.name2fixturedefs  # noqa: SLF001
    for index, name in enumerate(item.fixturenames):
        defined = known.get(name)
        if defined and defined[-1].scope == "function":
            return index
    return len(item.fixturenames)


@pytest.fixture(scope="session")
def sqlakit_db(sqlakit_base: Any) -> Any:  # noqa: ANN401
    """Return the database the marked tests run on.

    The one the models live on: their registry, which knows every alias, or the
    `Database` they were given in person. Override it for a project with no
    model layer, and return a dict or a list for a project on several databases
    with no registry between them.
    """
    if sqlakit_base is None:
        if not importable_db.is_configured:
            pytest.fail(
                "sqlakit has no database to test on. Define `sqlakit_base` with "
                "the base your models inherit, or `sqlakit_db` with the database "
                "itself.",
                pytrace=False,
            )
        return importable_db
    registry = getattr(sqlakit_base, "dbs", None)
    if (
        isinstance(getattr(sqlakit_base, "__db__", None), str)
        and registry is not None
        and registry.is_configured
    ):
        # The models look their aliases up there, so the schema and the
        # transactions cover every one of them, not the default alone.
        return registry
    database = getattr(sqlakit_base, "db", None)
    if database is None:
        pytest.fail(
            f"`sqlakit_base` returned `{sqlakit_base.__name__}`, which is not on "
            "the model layer and names no database. Return the base your models "
            "inherit, or define `sqlakit_db` instead.",
            pytrace=False,
        )
    return database


@pytest.fixture(scope="session")
def sqlakit_base() -> Any | None:  # noqa: ANN401
    """Return the declarative base of the models under test, or None.

    Every alias gets the tables of the models pointed at it, so a project on
    more than one database says this and nothing else.
    """
    return None


@pytest.fixture(scope="session")
def sqlakit_metadata() -> sa.MetaData | None:
    """Return the tables to create for the session, or None to create none.

    For metadata outside the model layer. None suits a suite running against a
    schema built elsewhere, by migrations or by a fixture of the project's own.
    """
    return None


@pytest.fixture(scope="session")
def sqlakit_schema(
    request: pytest.FixtureRequest,
    sqlakit_db: Any,  # noqa: ANN401
    sqlakit_base: Any,  # noqa: ANN401
    sqlakit_metadata: sa.MetaData | None,
) -> Iterator[None]:
    """Build the schema for the session, and take it down after.

    A model reaches the metadata when its module is imported, so a run of a
    few tests may leave out the tables of the models nothing imported.
    `sqlakit_models` names the packages to import every `models` module of
    first, the way `import_models` does:

    ```ini
    [pytest]
    sqlakit = true
    sqlakit_models = app
    ```

    Override it to build the schema another way, `Alembic` against a server
    `pytest-docker` started among them:

    ```python
    @pytest.fixture(scope="session")
    def sqlakit_schema(alembic_config, _postgres):
        alembic.command.upgrade(alembic_config, "head")
        yield  # the tests run here
        alembic.command.downgrade(alembic_config, "base")
    ```
    """
    for package in request.config.getini("sqlakit_models"):
        import_models(package)
    with _entered(_schema_blocks(sqlakit_db, sqlakit_base, sqlakit_metadata)):
        yield


@pytest.fixture(scope="session")
def sqlakit_seed(sqlakit_schema: None) -> None:  # noqa: ARG001 - after the schema
    """Write the rows every test starts from, committed, once for the session.

    A test's own writes roll back around it, and these stay:

    ```python
    @pytest.fixture(scope="session")
    def sqlakit_seed(sqlakit_schema):
        with db.transaction():
            Plan(name="free").save()
    ```

    It runs when the first marked test does, so a suite that needs no database
    writes nothing.
    """
    return


@pytest.fixture
def assert_queries(sqlakit_db: Any) -> AssertQueries:  # noqa: ANN401
    """Assert what a block asks of the databases under test.

    ```python
    @pytest.mark.db
    def test_the_page_costs_two_queries(assert_queries: AssertQueries):
        with assert_queries(2):
            User.query.order_by("name").page(limit=10)
    ```

    The same checks as `sqlakit.testing.assert_queries`: a count, an `at_most`
    ceiling, and `duplicates=False` to forbid a statement running twice. It
    watches what `sqlakit_db` returned, so a project whose databases are its
    own needs no registry for `using` to name one:

    ```python
    with assert_queries(1, using="replica"):
        build_report()
    ```
    """

    @contextmanager
    def asserted(
        count: int | None = None,
        *,
        at_most: int | None = None,
        duplicates: bool = True,
        using: Any = None,  # noqa: ANN401
    ) -> Generator[Recording]:
        require_expectation(count, at_most, duplicates)
        recording = Recording()
        with ExitStack() as stack:
            for one in _picked(sqlakit_db, _as_asked(using)):
                stack.enter_context(one.recording(into=recording))
            yield recording
        check(recording, count=count, at_most=at_most, duplicates=duplicates)

    return asserted


@pytest.fixture
def _sqlakit_transaction(
    request: pytest.FixtureRequest,
    sqlakit_db: Any,  # noqa: ANN401
    sqlakit_seed: None,  # noqa: ARG001 - requested so the rows are there
) -> Iterator[None]:
    with ExitStack() as stack:
        for block in _rolled_back(sqlakit_db, _asked_for(request)):
            if hasattr(block, "__aenter__"):
                pytest.fail(
                    f"`{request.node.name}` runs on an async database, whose "
                    "blocks are awaited. Write the test as `async def`.",
                    pytrace=False,
                )
            stack.enter_context(block)
        if _hidden(request):
            for one in _each(sqlakit_db):
                stack.enter_context(one.unbound())
        with _reported(request, sqlakit_db):
            yield


@pytest.fixture
async def _sqlakit_async_transaction(
    request: pytest.FixtureRequest,
    sqlakit_db: Any,  # noqa: ANN401
    sqlakit_seed: None,  # noqa: ARG001 - requested so the rows are there
) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        for block in _rolled_back(sqlakit_db, _asked_for(request)):
            # A synchronous database under a test that awaits something else,
            # a handler it runs in a worker thread among them.
            if hasattr(block, "__aenter__"):
                await stack.enter_async_context(block)
            else:
                stack.enter_context(block)
        if _hidden(request):
            for one in _each(sqlakit_db):
                stack.enter_context(one.unbound())
        with _reported(request, sqlakit_db):
            yield


@contextmanager
def _reported(request: pytest.FixtureRequest, db: Any) -> Generator[None]:  # noqa: ANN401
    """Record what the test runs, for the report, when one was asked for.

    The recording is named after the test and filed under the file it lives in,
    so the page groups a run the way the suite is laid out.
    """
    if request.config.stash.get(WHERE, None) is None:
        yield
        return
    node = request.node
    skip = request.config.getini("sqlakit_skip_queries_from")
    recording = Recording(label=_named(node))
    with ExitStack() as stack:
        for one in _each(db):
            stack.enter_context(
                one.recording(into=recording, stacks=True, skip_queries_from=skip)
            )
        yield
    request.config.stash[REPORT].append(
        (
            recording,
            str(node.path.relative_to(request.config.rootpath)),
            [mark.name for mark in node.iter_markers() if mark.name != MARKER],
        )
    )


def _named(node: pytest.Item) -> str:
    """Return the test as the suite names it, the class it is in included."""
    return "::".join(node.nodeid.split("::")[1:]) or node.name


@contextmanager
def _entered(blocks: list[Any]) -> Generator[None]:
    """Hold these blocks open, awaiting the ones that need it.

    An `asyncio` database builds its schema in a coroutine, and this fixture is
    not one: a session of its own runs it, which the tests never touch. They
    open their transactions on the loop `anyio` gives them.
    """
    if not any(hasattr(block, "__aenter__") for block in blocks):
        with ExitStack() as stack:
            for block in blocks:
                stack.enter_context(block)
            yield
        return

    stack = AsyncExitStack()
    loop = asyncio.new_event_loop()
    try:
        for block in blocks:
            loop.run_until_complete(stack.enter_async_context(block))
        yield
    finally:
        loop.run_until_complete(stack.aclose())
        loop.close()


def _schema_blocks(
    db: Any,  # noqa: ANN401
    base: Any,  # noqa: ANN401
    metadata: sa.MetaData | None,
) -> list[Any]:
    """Return the blocks that create the schema, one per database.

    The model layer knows which tables belong on which alias, so a registry
    gets one block per alias. Without it there is one metadata, and one
    database to put it on.
    """
    databases = _each(db)
    if len(databases) > 1:
        if metadata is None:
            pytest.fail(
                "`sqlakit_db` returned several databases, and the tables of each "
                "are the project's to create. Define `sqlakit_schema` with a "
                "`provisioned_tables()` per database, or `sqlakit_metadata` with "
                "the tables they share.",
                pytrace=False,
            )
        return [one.provisioned_tables(metadata) for one in databases]
    if base is not None:
        # A database of its own is where the models are pinned, and the alias
        # a registry knows it by is not how the base reaches it.
        aliases = db.aliases if hasattr(db, "transactions") else (None,)
        return [base.provisioned_tables(alias) for alias in aliases]
    if metadata is not None:
        return [db.provisioned_tables(metadata)]
    warnings.warn(
        "sqlakit creates no tables: neither `sqlakit_base` nor "
        "`sqlakit_metadata` is defined. Define one, or replace "
        "`sqlakit_schema` with the fixture that builds your schema.",
        stacklevel=2,
    )
    return []


def _asked_for(request: pytest.FixtureRequest) -> tuple[Any, ...]:
    """Return the databases the marker asks for, by name or in person."""
    marker = request.node.get_closest_marker(MARKER)
    return _as_asked(None if marker is None else marker.kwargs.get("using"))


def _as_asked(using: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """Return what ``using`` names, as the several it may be."""
    if using is None:
        return ()
    if isinstance(using, str) or not isinstance(using, (list, tuple, set, frozenset)):
        return (using,)
    return tuple(using)


def _hidden(request: pytest.FixtureRequest) -> bool:
    """Whether the test's own block is hidden from the code it calls.

    `sqlakit_unbound` says it for the suite, and `db(unbound=...)` on a test
    says it for that one.
    """
    marker = request.node.get_closest_marker(MARKER)
    asked = None if marker is None else marker.kwargs.get("unbound")
    if asked is None:
        return bool(request.config.getini("sqlakit_unbound"))
    return bool(asked)


def _each(db: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """Return the databases a project handed over, as several or as one.

    `sqlakit_db` returns one database, a registry of them, or several: a list,
    or a dict naming each, for a project with no registry between them.
    """
    if isinstance(db, Mapping):
        return tuple(db.values())
    if isinstance(db, (list, tuple)):
        return tuple(db)
    return (db,)


def _picked(db: Any, using: tuple[Any, ...]) -> list[Any]:  # noqa: ANN401
    """Return the databases a marker asks for, out of what a project handed over.

    A dict names them itself. A list names them by the alias each database
    carries, and a registry by the alias it holds one under.

    Raises:
        UnknownDatabaseError: if an alias belongs to none of them.

    """
    if not using:
        return list(_each(db))
    picked = []
    for asked in using:
        if not isinstance(asked, str):
            picked.append(asked)
        elif isinstance(db, Mapping):
            if asked not in db:
                raise UnknownDatabaseError(asked, tuple(db))
            picked.append(db[asked])
        elif isinstance(db, (list, tuple)):
            picked.append(_of_the_list(db, asked))
        else:
            picked.append(db[asked])
    return picked


def _of_the_list(databases: Sequence[Any], asked: str) -> Any:  # noqa: ANN401
    """Return the database of a list that carries this name.

    Raises:
        UnknownDatabaseError: if none of them carries it.

    """
    found = [one for one in databases if asked in one]
    if not found:
        aliases = tuple(alias for one in databases for alias in one.aliases)
        raise UnknownDatabaseError(asked, aliases)
    if len(found) > 1:
        pytest.fail(
            f"`sqlakit_db` returned {len(found)} databases named `{asked}`. "
            "Name them with `Database(url, alias=...)`, or return a dict "
            "naming each, so a marker can ask for one.",
            pytrace=False,
        )
    return found[0]


def _rolled_back(db: Any, using: tuple[Any, ...]) -> list[Any]:  # noqa: ANN401
    """Return the blocks that undo what a test writes.

    A marker with no ``using`` opens every database, which for most projects is
    the one they have. Naming one is how a project on several stops paying for
    a connection to each in the tests that read one.
    """
    if not using and len(_each(db)) == 1 and hasattr(db, "transactions"):
        # A registry opens every database it holds in one block.
        return [db.transactions(rollback=True)]
    return [one.transaction(rollback=True) for one in _picked(db, using)]
