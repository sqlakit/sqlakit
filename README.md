# SQLAKit

`SQLAKit` removes the boilerplate from `SQLAlchemy` applications. It manages
sessions and transactions for you, and adds a query builder with pagination
built in, `SQL` templates, an optional `Active Record` layer, debugging and
testing tools, etc. It supports both sync and async APIs and works with any
framework.

```console
$ pip install sqlakit
```

## A quick example

```python
import sqlalchemy as sa

from sqlakit import Database

from app.models import User

db = Database("postgresql+psycopg://localhost/app")


def get_user(email: str) -> User | None:
    return db.session.scalars(sa.select(User).where(User.email == email)).first()


@db.transaction
def get_or_create_user(email: str, name: str) -> User:
    user = get_user(email)
    if user is None:
        user = User(email=email, name=name)
        db.session.add(user)
    return user
```

Both functions use the same session, and you don't pass it between them. The
`@db.transaction` decorator opens it, and commits when the function returns.

Outside a block there is no session: `db.session` raises `MissingSessionError`
instead of silently opening a connection. `db.connection` works the same way
and raises `MissingConnectionError`.

## Connections and transactions

All blocks work as context managers and as decorators:

```python
with db.connect():  # a connection, with no transaction of its own
    ...

with db.transaction():  # commits at the end, rolls back on an exception
    ...

with db.autocommit():  # AUTOCOMMIT, no transaction held open
    ...
```

## SQL templates

Templates are SQL files with `:name` parameters, so they hold anything from a
one-line query to a report with window functions or a recursive CTE, and every
SQL tool reads them as SQL. Values are bound, so they never reach the SQL
text. What changes per call, an optional
condition or a sort order, is a macro such as `tpl.if_set(:team, team = :team)`.

### From a file

```sql
-- reports/by_team.sql
SELECT team, count(*) AS members
FROM users
WHERE joined_at > :since
GROUP BY team
```

```python
from pydantic import BaseModel

from sqlakit import Database

db = Database(DATABASE_URL, templates=BASE_DIR / "sql")


class TeamReport(BaseModel):
    team: str
    members: int


db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
# [TeamReport(team='red', members=2)]
```

`templates=` sets the directory to load templates from, and `typed()` sets the
type each row is returned as.

`SQLAKit` adds the template name to the SQL as a comment, so a slow query log
shows the source file of each query right away.

`sqlakit check` reads every template the project uses and reports a macro call
that can't work, before the query runs. See [template
validation](docs/sql.md#template-validation).

The [VS Code
extension](https://marketplace.visualstudio.com/items?itemName=sqlakit.sqlakit)
completes the macros, links a template name in Python to its file, marks the
same problems as you type, and shows a template rendered as the database gets
it. It runs `sqlakit-lsp`, a language server any other editor can run too. See
[editor support](docs/sql.md#editor-support).

### From a string

```python
db.sql.from_string("SELECT count(*) FROM users").scalars().one()
```

The same syntax, with no directory to configure.

## Query builder

The query builder wraps `select()`, so `where`, `join` and `order_by` work as
usual. On top of that it adds what `select` lacks: ordering by string,
limit-offset and cursor pagination, reading in batches, and bulk writes. It
works with any mapped class, with nothing to inherit from:

```python
db.query(User).where(User.is_active).order_by(User.name).all()
```

### Ordering by a string

`order_by` accepts a `field.direction` string, for example straight from a
query parameter. `SQLAKit` checks the field name against the model before it
builds any SQL, so an unknown field never reaches the database. Instead you get
`UnknownOrderFieldError`, and its message lists the fields the model allows:

```python
db.query(User).order_by("created_at.desc")  # or "name", "name.asc.nulls_last"
```

### Limit-offset pagination

`page()` also counts the total, so you can show "page 3 of 12":

```python
page = db.query(User).order_by("name").page(limit=20, offset=40)

page.items
page.total
page.has_next
```

### Cursor pagination

`cursor_page()` continues from a cursor, so it stays fast at any depth. There
is no total. Instead you get cursors to the next and previous pages:

```python
feed = db.query(User).order_by("created_at.desc").cursor_page(limit=20)

feed.items
feed.next_cursor
feed.previous_cursor
```

## Testing

A test runs inside a transaction that rolls back at the end, so nothing the
code under test writes is actually committed. `assert_queries` checks how
many statements a block runs:

```python
with db.transaction(rollback=True), db.assert_queries(2):
    render(dashboard)
```

## Debugging queries

`recording()` shows what ran, how long it took, and what ran more than once:

```python
import logging

logger = logging.getLogger(__name__)

with db.recording("GET /users", logger=logger) as record:
    list_users()

record.count
record.milliseconds
record.duplicates
```

With `logger=` `SQLAKit` logs one line at the end of the block. The log level
depends on the numbers: more statements and more repeats mean a higher level.

With `echo=True` the block prints each statement, formatted and with repeats
marked:

```python
with db.recording(echo=True):
    list_users()
```

```sql
3 queries in 0.0ms (2 repeated)
   1    0.0ms
      SELECT users.team_id
      FROM users
      ORDER BY users.name ASC
   2    0.0ms  ↑ same as 3 (2 times in all)
      SELECT teams.id AS teams_id,
             teams.name AS teams_name
      FROM teams
      WHERE teams.id = ?
   3    0.0ms  ↑ same as 2 (2 times in all)
      SELECT teams.id AS teams_id,
             teams.name AS teams_name
      FROM teams
      WHERE teams.id = ?
```

You can spot the N+1 right away: one query for the users and two identical
ones for the teams. Formatting needs the `sqlakit[debug]` extra, and if the
project has `rich`, the output is colored too.

### The debug server

[`sqlakit-debugserver`](https://github.com/sqlakit/sqlakit-debugserver) serves a page that fills as the recordings
arrive. It is a package of its own:

```console
$ pip install sqlakit-debugserver
$ sqlakit-debugserver

SQLAKit debug server on http://localhost:5555

Send recordings to it:

  │  from sqlakit_debugserver import DebugServer
  │
  │  with db.recording("GET /users", send_to=DebugServer("localhost", 5555)):
  │      list_users()
```

![The SQLAKit debug server](docs/assets/debugserver.png)

The recordings are listed on the left, the one you pick opens on the right:
the SQL highlighted, the parameters ready to paste, the repeats counted, and
the line of your code behind every statement. Search by `table:`, `kind:`,
`ms:>50` or `repeated:>0`, and one server watches as many applications as you
point at it.

`pytest --sqlakit-report` writes the same page for a test run, as a file that
opens without a server: the test is the label, and each statement carries the
line of the test that ran it.

## The registry

To avoid passing a `Database` from module to module, configure the registry
once at startup:

```python
# app/main.py
from sqlakit import db

db.configure("postgresql+psycopg://localhost/app")
```

Any other module just imports it:

```python
# app/users.py
from sqlakit import db

from app.models import User


def list_users() -> list[User]:
    return db.query(User).order_by("name").all()
```

## More than one database

The registry can hold several databases. Configure them under aliases, and pick
one per block:

```python
from sqlakit import db

db.configure(
    {
        "default": {"url": PRIMARY_URL},
        "replica": {"url": REPLICA_URL},
    }
)

with db.using("replica").connect():
    list_users()  # the models read the replica
```

## Active Record

An instance saves and deletes itself, and the query is available on the class.
A model on the registry needs no wiring of its own:

```python
from sqlalchemy.orm import Mapped, mapped_column

from sqlakit import db
from sqlakit.orm import Model


class Note(Model):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]


with db.transaction():
    note = Note(text="ada").save()

    Note.query.where(Note.text == "ada").all()
    note.delete()
```

A model that belongs on another database in the registry names its alias with
`__db__ = "warehouse"`. With a `Database` of your own, `set_db()` binds the
model to it. Either goes on a base class, and every model under it inherits
the binding.

This layer is optional. Everything else works on plain `SQLAlchemy` models, so
if saving belongs in your repositories or services, skip `sqlakit.orm`
entirely. `SQLModel` classes are `SQLAlchemy` models, and work either way: the
[examples](examples/) show both.

## The async API

The async API is identical: the same classes, the same methods. Only the import
changes. It needs the `sqlakit[asyncio]` extra:

```python
from sqlakit.asyncio import Database

db = Database("postgresql+psycopg://localhost/app")

async with db.transaction():
    page = await db.query(User).order_by("name").page(limit=20)
```

The builder itself stays synchronous: `where` and `order_by` run no SQL, so
there is nothing to await.

## `FastAPI` integration

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.models import User
from sqlakit.asyncio import Database

db = Database("postgresql+psycopg://localhost/app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await db.dispose()  # close the pool on shutdown


app = FastAPI(lifespan=lifespan)


class UserCreate(BaseModel):
    name: str
    team: str = ""


class UserResponse(BaseModel, from_attributes=True):
    id: int
    name: str
    team: str


@app.post("/users", status_code=201)
@db.transaction  # one transaction, committed when the handler returns
async def create_user(payload: UserCreate) -> UserResponse:
    user = User(name=payload.name, team=payload.team)
    db.session.add(user)
    await db.session.flush()  # INSERT now, the id is needed for the response
    return UserResponse.model_validate(user)
```

No `Depends(get_session)`, no session factories, and no `async with` in the
handler.

Use the `Database` from `sqlakit.asyncio` here. With the sync one the block
closes before the async handler runs, and the handler fails with
`MissingConnectionError`.

There is nothing to open at startup: `SQLAKit` creates the engine on first use.
On shutdown, `dispose()` closes the pool.

## Documentation

[Getting started](docs/getting-started.md) builds a database, a model and a
test from an empty file. The rest is under [`docs/`](docs/):
[compared to SQLAlchemy](docs/comparison.md), [queries](docs/queries.md),
[SQL templates](docs/sql.md), [models](docs/models.md),
[testing](docs/testing.md), [debugging](docs/debugging.md),
[multiple databases](docs/routing.md) and [the reference](docs/reference.md).
Complete example apps live in [`examples/`](examples/), and each one is run by
the test suite.

What changed in each version is in the [changelog](CHANGELOG.md).
