# Testing

Tests run against a real database. Which one is up to you: `SQLite` is fast,
needs nothing installed, and supports everything this page uses, savepoints
included. If your code depends on the dialect, its types, its constraints or
its migrations, test on the database you deploy on. Many suites do both, and
this page works the same either way.

!!! note "Why savepoints work on `SQLite`"

    The `SQLite` driver in the standard library does not issue `BEGIN` on its
    own, which breaks `SAVEPOINT` and nested transactions. `Database` applies
    the workaround from `SQLAlchemy`'s pysqlite documentation for
    `sqlite+pysqlite` and `sqlite+aiosqlite`, and skips it under `AUTOCOMMIT`,
    where opening a transaction would defeat the purpose of the mode.

Running against a database works because of two guarantees: every test leaves
the database as it found it, and the code under test runs the same way it does
in production.

`provisioned_tables()` and `transaction(rollback=True)` provide both, and the
plugin below calls them for you:

- **`provisioned_tables()`** creates the schema once for the whole run and drops
  it afterwards.
- **`transaction(rollback=True)`** wraps each test in a real transaction and
  rolls it back at the end. Everything inside runs on one connection and cannot
  commit past it: both the functions the test calls and the blocks those
  functions open.

Nested blocks join the enclosing one by default, so a single `rollback=True` is
enough. `join_nested=True` is already the default, and `join_nested=False`
moves the code under test off the test's connection, where it commits for real
and the rollback cannot undo it.

## The pytest plugin

The library installs a `pytest` plugin. Turn it on, and say which models the
project has:

```ini title="pytest.ini"
[pytest]
sqlakit = true
```

```python title="conftest.py"
import pytest

from app.models import Model


@pytest.fixture(scope="session")
def sqlakit_base() -> type[Model]:
    return Model
```

A test marked `db` runs in a transaction that rolls back. Every other test
connects to nothing, and reaching for a session there raises
`MissingSessionError`:

```python
@pytest.mark.db
def test_renaming_a_user() -> None:
    user = User(name="ada").save()

    rename(user.id, "grace")

    user.refresh()

    assert user.name == "grace"
```

`refresh()` reads the row again, discarding what the session cached. Without it
the assertion checks nothing: the code under test ran on the test's session, so
`user` is the instance it changed.

`provisioned_tables()` creates the schema once for the session and drops it
after, on the database the models live on, and every marked test runs inside
`transaction(rollback=True)`. The marker works on a module and on a class, and
the closest one wins:

```python
pytestmark = pytest.mark.db


@pytest.mark.db(using="warehouse")
class TestTheReports:
    def test_a_report_is_written(self) -> None: ...
```

### Async tests

`anyio` runs a coroutine test when the `anyio_backend` fixture reaches it.
`autouse=True` sends it to every test, so none of them carries
`@pytest.mark.anyio`:

```python
@pytest.fixture(scope="session", autouse=True)
def anyio_backend() -> str:
    return "asyncio"
```

Write it either way. Without it `anyio` runs each async test once per backend
it finds installed, and `SQLAlchemy`'s async drivers need `asyncio`. The plugin
needs nothing else: it awaits what has to be awaited.

A test on a synchronous database can be `async def` as well, for a reason of
its own: it awaits an HTTP client, or a handler it hands to a worker thread.
The marker works there too. The plugin awaits the blocks an async database
opens and enters the rest as they are, so what the database is decides, not
how the test is written.

### Database selection

The schema is created once for the session, on every database. A transaction is
opened for every test, on every database, and `using` narrows that:

```python
@pytest.mark.db(using="warehouse")
def test_the_quarterly_report() -> None: ...
```

A project on three databases opens three transactions per test without it. It
takes an alias, a list of them, or a database itself, the same as
`assert_queries(using=...)`. A test that reaches for a database its marker
leaves out raises `MissingSessionError`.

### Schema creation

`sqlakit_schema` is the `provisioned_tables()` call. A suite with a schema of
its own replaces the fixture:

```python
@pytest.fixture(scope="session")
def sqlakit_schema() -> Iterator[None]:
    with db.transaction() as conn:
        conn.execute(sa.text(SCHEMA_SQL))
    yield
    with db.transaction() as conn:
        conn.execute(sa.text("DROP TABLE users"))
```

Migrations go here, and so does waiting for a server the tests start.
[Migrations instead of `create_all`](#migrations-instead-of-create_all) writes
that fixture out, and [starting a server](#server-startup) brings one up
with `pytest-docker`. An `asyncio` project writes it as an async fixture, under
the same name.

`sqlakit_base` is a shortcut for the two things the plugin takes from it, the
database the models live on and the tables to create. A project that creates
its own schema names the database instead, and the fixture creates nothing:

```python
@pytest.fixture(scope="session")
def sqlakit_db() -> Database:
    return db
```

### Projects without a model layer

A project on plain mapped classes, `SQLModel` among them, has no base to hand
over. Name the database and the metadata instead:

```python title="conftest.py"
import pytest
import sqlalchemy as sa

from sqlakit import Database

from app.db import db
from app.models import Base


@pytest.fixture(scope="session")
def sqlakit_db() -> Database:
    return db


@pytest.fixture(scope="session")
def sqlakit_metadata() -> sa.MetaData:
    return Base.metadata
```

The plugin creates the tables with `db.provisioned_tables(Base.metadata)`, the
same method the model layer uses.

The marker and the rollback work the same, and the tests read through the
database rather than through a model:

```python
@pytest.mark.db
def test_a_user_is_written() -> None:
    db.session.add(User(name="ada"))
    db.session.flush()

    assert db.query(User).count() == 1
```

### A base for each database

`sqlakit_base` names one base, so a project with a base per database names the
registry and builds both schemas:

```python title="conftest.py"
import pytest

from sqlakit import Databases

from app.db import db
from app.models import Model, WarehouseModel


@pytest.fixture(scope="session")
def sqlakit_db() -> Databases:
    return db


@pytest.fixture(scope="session")
def sqlakit_schema(sqlakit_db: Databases) -> Iterator[None]:
    with Model.provisioned_tables(), WarehouseModel.provisioned_tables():
        yield
```

The bases share the registry, and each says where its models live: `Model`
takes the default and `WarehouseModel` carries `__db__ = "warehouse"`. The
marker opens every database of the registry, and `using` still narrows it to
one.

### Databases with no registry between them

A project that builds its databases itself, and pins its models to them with
`set_db()`, names them in the fixture:

```python title="conftest.py"
from collections.abc import Iterator

import pytest

from sqlakit import Database

from app.db import orders_db, reporting_db
from app.models import OrderModel, ReportModel


@pytest.fixture(scope="session")
def sqlakit_db() -> dict[str, Database]:
    return {"orders": orders_db, "reporting": reporting_db}


@pytest.fixture(scope="session")
def sqlakit_schema(sqlakit_db: dict[str, Database]) -> Iterator[None]:
    with OrderModel.provisioned_tables(), ReportModel.provisioned_tables():
        yield
```

The marker opens a transaction on each and rolls them all back, and `using`
picks one by its key:

```python
@pytest.mark.db(using="reporting")
def test_the_quarterly_report() -> None: ...
```

A list works as well, `[orders_db, reporting_db]`. There `using` picks by the
name each database carries, so build them as
`Database(url, alias="reporting")`. Two unnamed databases are both `default`,
and a marker cannot pick between them: the plugin says so rather than guessing.

The keys of a dict name the databases for the marker alone. A recorded
statement carries the name its database was built with, whatever the fixture
calls it, so name a database you want to tell apart in a report.

The tables of each database are the project's to create, as above. A dict or a
list with `sqlakit_metadata` instead creates the same tables on every database,
as a suite over shards wants.

### Rollback limits

Each database rolls back its own transaction, on its own connection. Two
consequences follow:

- A test that writes to one database and reads from another sees only what the
  second one holds. No transaction spans both, and no isolation level changes
  that.
- A replica alias is a second connection, and it cannot see the test's
  uncommitted rows even when it points at the same database.

The second point breaks the suite as soon as a router appears in the project:
reads go to `replica`, the test's rows are never committed, and every read
comes back empty. Keep routers off in tests. A fresh registry has none, and
the fixture below clears them after each test:

```python
@pytest.fixture(autouse=True)
def _no_routers() -> Iterator[None]:
    yield
    db.route()  # reads and writes both go to default again
```

A model that set its database through `__db__` keeps it: `route()` clears the
routing policy, not the model's own setting. Test the policy by checking where
a model resolves, not by reading data:

```python
def test_reads_go_to_the_replica() -> None:
    db.route(reads_go_to_the_replica)

    assert User.db is db["replica"]
```

### Hidden blocks

The marker opens a block around the whole test, and the code the test calls can
use it. Production wraps that code in nothing, so a handler that never opened a
block of its own passes here and raises `MissingSessionError` there.

Hide the block in a test that calls something production calls with nothing
around it: a handler, a consumer, a scheduled job, a command, a view whose
middleware opens no block. A test that writes its own rows and reads them back
does not need it, since nothing under it opens blocks either way.

To find them, turn it on for a run and read the failures:

```ini title="pytest.ini"
[pytest]
sqlakit = true
sqlakit_unbound = true
```

A failure inside your own code is the bug this finds: that code needs
`with db.transaction():`. A failure on a line of the test is the test reading
in its own body, which now needs a block of its own:

```python
@pytest.mark.db
def test_the_handler_writes_an_order() -> None:
    handle(event)

    with db.connect():
        assert Order.query.count() == 1
```

The transaction is still open and still rolls back. What changed is that the
code under test opens its own block, as it does in production.

A test that writes its rows inline asks for the block back, which is also how a
suite moves over one test at a time:

```python
@pytest.mark.db(unbound=False)
def test_a_row_written_here() -> None:
    Order(what="a mug").save()

    assert Order.query.count() == 1
```

Leave the setting off, and `db(unbound=True)` hides the block for one test,
`with db.unbound():` for one call.

## Seed data

A fixture that writes rows for one test is like any other: it runs inside the
test's transaction and rolls back with it.

```python
@pytest.fixture
def team() -> Team:
    return Team(name="red").save()
```

For rows every test starts from, `sqlakit_seed` writes them once for the
session, committed, so a test's own rollback leaves them alone:

```python
@pytest.fixture(scope="session")
def sqlakit_seed(sqlakit_schema: None) -> None:
    with db.transaction():
        Plan(name="free").save()
```

It runs when the first marked test does, so a suite that needs no database
writes nothing. An `asyncio` project writes the same fixture as an async one,
and the plugin awaits it.

For rows a few tests share rather than all of them, open a transaction around
the module or the class. The tests nest inside it as savepoints, and it rolls
back when they are done:

```python
@pytest.fixture(scope="module", autouse=True)
def _seeded(sqlakit_schema: None) -> Iterator[None]:
    with db.transaction(rollback=True):
        Plan(name="trial").save()
        yield
```

Ask for `sqlakit_schema`, or the rows go in before the tables exist.

## Tables missing from the schema

`provisioned_tables()` creates the tables the metadata holds. A model whose
module was never imported is not in the metadata, so its tables are missing,
and the failure looks like a bug in the test. A run of a few tests is where it
shows: the rest of the suite imported the model, and these tests don't.

If your application keeps models next to the features they belong to, name
the package, and the plugin imports every `models` module under it before it
creates the tables:

```ini
[pytest]
sqlakit = true
sqlakit_models = app
```

Without the plugin, import them in your own fixture:

```python
from sqlakit import import_models


@pytest.fixture(scope="session")
def _db_schema() -> Iterator[None]:
    import_models("app")
    with Model.provisioned_tables():
        yield
```

## Server startup

Migrations are usually written for the database you deploy on, not for
`SQLite`. `pytest-docker` starts one for the session, and the schema fixture
waits for it before running anything:

```yaml title="tests/docker-compose.yaml"
services:
  postgres:
    image: postgres:18
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app_test
    ports:
      - "7432:5432"
```

```python
import contextlib
import pathlib
from collections.abc import Iterator

import pytest

from sqlakit import Database

URL = "postgresql+psycopg://app:app@127.0.0.1:7432/app_test"


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig: pytest.Config) -> Iterator[pathlib.Path]:
    with contextlib.chdir(pytestconfig.rootpath):
        yield pytestconfig.rootpath / "tests" / "docker-compose.yaml"


def _db_is_up() -> bool:
    with Database(URL) as probe:
        return probe.ping()


@pytest.fixture(scope="session")
def postgres(docker_services: pytest.FixtureRequest) -> str:
    """Return the URL of a server that answers."""
    docker_services.wait_until_responsive(timeout=60.0, pause=1.0, check=_db_is_up)
    return URL
```

`docker compose` reads the paths inside the file against the working
directory, so the block holds the tests at the project root however they were
started. The container takes a while to accept connections, and
`wait_until_responsive` holds the first test back until it does. Ask for the
fixture where the schema is created:

```python
@pytest.fixture(scope="session")
def sqlakit_schema(
    postgres: str, alembic_config: alembic.config.Config
) -> Iterator[None]: ...
```

Point the application at the same URL, in `conftest.py` or in the settings the
tests load, or the migrations and the tests will run on two different servers.
If the application configures `sqlakit.db` itself, see the next section.

## Point the application at the test database

If your application sets up its database itself, at startup or on import, use
`db.override()` to point it at the test database. With `sqlakit.db`, return the
registry from `sqlakit_db`:

```python title="conftest.py"
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from sqlakit import Database, Databases, db
from sqlakit.orm import Model


@pytest.fixture(scope="session")
def sqlakit_base() -> type[Model]:
    return Model


@pytest.fixture(scope="session")
def sqlakit_db() -> Iterator[Databases]:
    test_db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with test_db, db.override(test_db):
        yield db
```

Inside the block, `db.session`, `db.transaction()`, the models and decorated
functions all use the test database. Every thread sees it, so a server running
in its own thread does too. For another alias, pass `alias="replica"`.

A `Database` of your own has `override()` too:

```python title="conftest.py"
from app.db import db  # db = Database(settings.DATABASE_URL)


@pytest.fixture(scope="session")
def sqlakit_db() -> Iterator[Database]:
    test_db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with test_db, db.override(test_db):
        yield db
```

`override()` doesn't close the database. The `with test_db` does.

A function decorated through a dependency-injection proxy keeps the database
it got at import. Decorate it with
[`transaction`](context.md#databases-found-at-call-time) instead.

## Migrations instead of `create_all`

If your application has migrations, test the schema you'll actually deploy.
`sqlakit_schema` is where they run, once per session, and the rollback around
each test stays as it is.

If you don't pass the test's connection to `Alembic`, it opens one of its own
and the migration runs outside your transaction. The rollback can't undo it,
and the schema outlives the run. So your `env.py` needs to accept a connection
from outside. The rest of this section depends on it:

```python title="migrations/env.py"
from alembic import context

connection = context.config.attributes.get("connection")

if connection is not None:
    # Handed in from outside: by the tests, or by a script.
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = sa.create_engine(DATABASE_URL, poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
```

`connection` is the same name on both sides: the key the fixtures below put
into `config.attributes`, and the argument of `context.configure`.

Two fixtures follow: one reads `alembic.ini`, the other passes the connection.
`Alembic` resolves `script_location` from the working directory, so build the
config from the project root and stay there while it runs. Otherwise your tests
pass when run from the root and fail from anywhere else.

```python
import contextlib
from collections.abc import Iterator

import alembic.command
import alembic.config
import pytest


@pytest.fixture(scope="session")
def alembic_config(pytestconfig: pytest.Config) -> Iterator[alembic.config.Config]:
    with contextlib.chdir(pytestconfig.rootpath):
        yield alembic.config.Config(pytestconfig.rootpath / "alembic.ini")


@pytest.fixture(scope="session")
def sqlakit_schema(alembic_config: alembic.config.Config) -> Iterator[None]:
    with db.transaction() as conn:
        alembic_config.attributes["connection"] = conn
        alembic.command.upgrade(alembic_config, "head")
    yield
    with db.transaction() as conn:
        alembic_config.attributes["connection"] = conn
        alembic.command.downgrade(alembic_config, "base")
```

`Alembic` is synchronous, so on an async database the migrations run through
`run_sync`:

```python
@pytest.fixture(scope="session")
async def sqlakit_schema(
    alembic_config: alembic.config.Config,
) -> AsyncIterator[None]:
    def upgrade(connection: sa.Connection) -> None:
        alembic_config.attributes["connection"] = connection
        alembic.command.upgrade(alembic_config, "head")

    def downgrade(connection: sa.Connection) -> None:
        alembic_config.attributes["connection"] = connection
        alembic.command.downgrade(alembic_config, "base")

    async with db.transaction() as conn:
        await conn.run_sync(upgrade)
    yield
    async with db.transaction() as conn:
        await conn.run_sync(downgrade)
```

`run_sync` passes the function the underlying synchronous connection.
`Alembic` requires a synchronous one, and `env.py` reads it as shown above.

## Query counting

`assert_queries` is a [recorder](debugging.md) with an assertion around it. It
counts the same way and prints the same statements on failure. It lives on the
database, next to `recording()`:

```python
@pytest.mark.db
def test_the_list_page_costs_two_queries() -> None:
    with db.assert_queries(2):
        User.query.order_by("name").page(limit=10)
```

The checks work alone or combined: an exact number, an `at_most` ceiling, and
`duplicates=False` to forbid repeats, which catches an N+1.

```python
with db.assert_queries(at_most=5):
    ...  # an upper bound instead of an exact number
with db.assert_queries(duplicates=False):
    ...  # no repeats allowed, catches N+1
with db.assert_queries(3, duplicates=False):
    ...
```

The plugin has a fixture of the same name, which watches whatever `sqlakit_db`
returned. A project whose databases are its own needs no registry for `using`
to name one:

```python
from sqlakit.testing import AssertQueries


@pytest.mark.db
def test_the_report_reads_one_database(assert_queries: AssertQueries) -> None:
    with assert_queries(1, using="reporting"):
        build_report()
```

`db.assert_queries` watches the database it was called on. To watch several at
once, or the configured registry, use the function of the same name from
`sqlakit.testing`:

```python
from sqlakit.testing import assert_queries

with assert_queries(2):  # every database `sqlakit.db` has
    ...
with assert_queries(2, using="warehouse"):  # one of them, by alias
    ...
```

On failure it prints everything that ran, with repeats cross-referenced:

```sql
AssertionError: 4 queries, expected 2

   1    0.4ms  SELECT players.id, players.team_id FROM players ORDER BY players.id ASC
   2    0.2ms  SELECT teams.id, teams.name FROM teams WHERE teams.id = ?
              ↑ same as 3, 4 — 3 times in all
   3    0.2ms  SELECT teams.id, teams.name FROM teams WHERE teams.id = ?
              ↑ same as 2, 4 — 3 times in all
   4    0.2ms  SELECT teams.id, teams.name FROM teams WHERE teams.id = ?
              ↑ same as 2, 3 — 3 times in all
```

It needs two things in place:

- **A database to watch.** `db.assert_queries` takes the one it was called on.
  The standalone function takes the whole registry, unless `using` names a
  single database.
- **A test that has a database at all**, meaning the `db` marker above it.
  Without the marker nothing is connected, and the code under test raises
  instead of counting zero.

The block is a plain `with` in both cases, under `asyncio` as well, because
recording only listens and runs nothing itself. When you want the numbers
themselves rather than an assertion, use `db.recording()`, the recorder this
is created on.

### Query counts across databases

The standalone `assert_queries` watches every database in the registry, so a
block that touches two databases gets one combined count. To watch a single
one, pass its alias or the database itself:

```python
from sqlakit.testing import assert_queries

with assert_queries(2):
    User.query.count()
    Event.query.count()

with assert_queries(1, using="warehouse"):
    build_report()
```

`db.assert_queries` watches one database. On the registry it covers them all.

A recording tracks which database ran each statement, so a test can prove that
nothing reached the warehouse:

```python
with db.recording() as record:
    register_user("ada@example.com")

assert record.databases == ("default",)
```

## Refreshing an instance

The code under test writes on the test's connection, so the rows are already
in the database. The instance your test holds, though, still carries the
values it was loaded with. Read the row again:

```python
@pytest.mark.db
def test_revoking_a_token(token: Token) -> None:
    revoke(token.id)

    token.refresh()

    assert token.is_revoked
```

## Blocks inside a test

A rolled-back block is an ordinary transaction, so the code under test behaves
as it does in production. Inside it:

- A nested `transaction()` joins the test's transaction on the same connection
  instead of opening its own. Its commit only flushes, and only the test's
  block commits or rolls back for real.
- `autocommit()` joins as well and commits nothing inside the block: it uses
  the connection the test bound, so everything written rolls back with the
  rest. It opens an `AUTOCOMMIT` connection of its own only when no connection
  is bound.
- A nested `transaction(rollback=True)` takes a savepoint, so it undoes its own
  writes and leaves the test's alone.
- A session of an enclosing block that commits mid-block, which a factory or a
  handler does, releases the savepoint the inner block runs in. The work is
  kept and the block ends quietly, because one session owns the savepoints of
  a connection and releases them in the order they were taken.
- A session that rolls *itself* back, through `session_factory()` and then
  `rollback()`, ends the whole transaction. The block then raises
  `TransactionRolledBackError`, because there is nothing left to commit.
  Production behaves the same way. A block that should fail on its own needs
  `transaction(savepoint=True)`.

## Tests without the plugin

The same marker, written by hand:

```python title="conftest.py (by hand)"
from collections.abc import Iterator

import pytest

from app.db import db
from app.models import Model


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: the test needs a database")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give a database to the marked tests and to nothing else."""
    for item in items:
        if isinstance(item, pytest.Function) and item.get_closest_marker("db"):
            item.fixturenames.insert(0, "_db_transaction")


@pytest.fixture(scope="session")
def _db_schema() -> Iterator[None]:
    with Model.provisioned_tables():
        yield


@pytest.fixture
def _db_transaction(_db_schema: None) -> Iterator[None]:
    with db.transaction(rollback=True):
        yield
```

An unmarked test never asks for `_db_transaction`, so nothing connects, and
`_db_schema` runs only when some test does ask.

### Several databases by hand

The schema is a `provisioned_tables()` per alias, and each database gets the
tables of the models that point at it. An association table lands on the same
database as the rows it joins:

```python
@pytest.fixture(scope="session")
def _db_schema() -> Iterator[None]:
    with Model.provisioned_tables(), Model.provisioned_tables("warehouse"):
        yield
```

Then open a transaction on each one. `transactions()` does that for every
database in the registry:

```python
@pytest.fixture
def _db_transaction(_db_schema: None) -> Iterator[None]:
    with db.transactions(rollback=True):
        yield
```

Your test can now write to either database through its model, and both
transactions roll back when it ends:

```python
@pytest.mark.db
def test_a_signup_is_recorded() -> None:
    register_user("ada@example.com")

    assert User.query.count() == 1
    assert Event.query.count() == 1  # `Event.__db__` is "warehouse"
```

### Async tests without the plugin

The fixtures become async, because the transaction has to open on the loop the
test runs on. The marker and the hook are the same:

```python title="conftest.py (by hand, asyncio)"
from collections.abc import AsyncIterator

import pytest

from app.db import db
from app.models import Model


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: the test needs a database")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give a database to the marked tests and to nothing else."""
    for item in items:
        if isinstance(item, pytest.Function) and item.get_closest_marker("db"):
            item.fixturenames.insert(0, "_db_transaction")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def _db_schema() -> AsyncIterator[None]:
    async with Model.provisioned_tables():
        yield


@pytest.fixture
async def _db_transaction(_db_schema: None) -> AsyncIterator[None]:
    async with db.transaction(rollback=True):
        yield
```

`pytest` reads `item.fixturenames` at collection time, and the fixtures set up
in that order. `insert(0, ...)` puts the transaction before the ones the test
asked for, so a fixture that writes rows writes them inside it. Appending
leaves those rows outside the rollback.

A fixture of a wider scope, one that seeds a whole module, needs the
transaction after it instead. The plugin works that position out for you.

Mark your tests as `anyio`, or make the `anyio_backend` fixture above
`autouse=True` and mark none of them:

```python
@pytest.mark.anyio
@pytest.mark.db
async def test_renaming_a_user() -> None:
    user = await User(name="ada").save()

    await rename(user.id, "grace")

    await user.refresh()

    assert user.name == "grace"
```

Next: [debugging](debugging.md), for watching the same queries outside a test.
