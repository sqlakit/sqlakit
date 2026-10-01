# Context

Your code opens a block. Everything called inside it uses the same connection
and the same session, so you don't have to pass either one between functions.

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

Every block works as a context manager and as a decorator, with or without
parentheses:

| | for |
| --- | --- |
| [`connect()`](#connect) | reads, and code that commits on its own |
| [`transaction()`](#transaction) | writes that must commit or roll back as one unit |
| [`autocommit()`](#autocommit) | read paths, and statements that cannot run inside a transaction |
| [`session_factory()`](#session-factory) | code written against a session object, connected on first use |

Outside a block there's no connection and no session. Accessing them raises an
error instead of silently opening a connection:

```python
db.connection  # MissingConnectionError
db.session  # MissingSessionError
```

## connect() {#connect}

Binds a connection for the block, or reuses the one already bound.

```python
with db.connect() as conn:
    conn.execute(sa.text("SELECT 1"))
```

The block doesn't create a session up front. The first time you access
`db.session`, it creates one on the same connection:

```python
with db.connect():
    db.in_session()  # False, no session yet
    db.session  # the session is created here
    db.in_session()  # True
```

So a block that only uses `db.connection` never creates a session.

**It commits nothing by itself.** The block only provides a connection.
Committing is up to you:

```python
with db.connect():
    db.session.add(User(name="ada"))  # lost when the session closes

with db.connect():
    db.session.add(User(name="ada"))
    db.session.commit()  # kept
```

`save()` from the model layer and `create()` on a query are the exception: in
a block with no transaction they commit on their own.

## transaction() {#transaction}

Opens a connection and a transaction, commits when the block ends, and rolls
back if the block raises.

```python
with db.transaction():
    db.session.add(User(name="ada"))  # the block commits this


@db.transaction
def import_users(rows: list[Row]) -> None: ...
```

`db.in_transaction()` returns whether a transaction is open.

### Nested blocks

A nested block reuses the connection that is already bound instead of opening
a second one:

```python
with db.transaction() as conn:
    with db.connect() as inner:
        assert inner is conn
```

A second connection would run in its own transaction: it wouldn't see the
outer block's writes, it would take another connection from the pool, and it
could deadlock on rows the outer transaction holds.

It works the other way round as well. A `transaction()` inside `connect()` or
`session_factory()` runs on the connection those blocks bound, and commits
what is outstanding on it when it ends:

```python
with db.connect() as conn:
    with db.transaction() as inner:
        assert inner is conn  # one connection, one transaction on it
```

Two blocks keep a connection of their own. `transaction(join_nested=False)`
says so outright, and a block inside `autocommit()` has to: an `AUTOCOMMIT`
connection runs no transaction to take part in.

The outermost block owns the commit. Nested blocks run no statements of their
own, so wrapping a helper in `transaction()` costs nothing. Each nested block
still opens its own session, so code you call can't close or roll back the
session of the block above it.

### Nested block failures

Without a savepoint a nested block cannot undo only its own work: if it fails,
the whole transaction rolls back. `savepoint=True` changes that:

```python
with db.transaction():
    db.query(Note).create(text="kept")

    with suppress(ValidationError), db.transaction(savepoint=True):
        db.query(Note).create(text="undone")
        raise ValidationError
```

It's off by default: every savepoint costs a round trip, and it doesn't help
when a failure should end the whole transaction anyway. On `PostgreSQL` there
is a second cost. Past 64 subtransactions the backend's `subxid` cache
overflows, reads start going to `pg_subtrans`, and that slows down the whole
cluster, not just the one transaction.

### Independent nested blocks

`join_nested=False` does the opposite. Set it on a block, and its nested
blocks open connections of their own instead of joining it. They don't see
its writes, and their writes survive its rollback.

```python
with db.transaction(join_nested=False):
    with db.transaction():
        db.query(Audit).create(text="the attempt was made")

    raise RuntimeError  # the audit row stays
```

The typical use is auditing, where the record of an attempt has to survive
even when the attempt fails. The cost is a second connection from the pool and
a second transaction that can deadlock with the first, so only set the flag on
the blocks that need it.

### Commits despite an error

```python
@db.transaction(commit_on_error=ContentBlockedError)
def moderate(post_id: int) -> None:
    save_verdict(post_id, check(post_id))
    raise ContentBlockedError
```

The exception still propagates, and the block commits everything written
before it.

### Retries

A retry has to re-run the block, and a `with` statement cannot do that. So
`transaction(retry_on=...)` works only as a decorator. If you use it with
`with`, the type checker flags it right away, and at runtime it raises
`RetryNotSupportedError`:

```python
def is_conflict(exc: BaseException) -> bool:
    return isinstance(exc, sa.exc.DBAPIError) and exc.orig.sqlstate in {
        "40001",
        "40P01",
    }


@db.transaction(retry_on=is_conflict, max_retries=3)
def transfer(from_id: int, to_id: int, amount: int) -> None: ...
```

Make the block safe to run twice. Only the block that owns the transaction
retries. Called from inside another transaction it runs once, because the
snapshot that caused the conflict is fixed for the whole transaction.

`max_retries` counts the extra attempts, so `max_retries=3` runs the block at
most four times. Between attempts it waits `backoff(attempt)` seconds,
counting from zero. The default is roughly 0.1, 0.2 and 0.4 seconds, each with
jitter, so workers that collided once don't all retry at the same moment. If
you need longer waits, pass a function of your own. In tests, `lambda _: 0.0`
skips the waiting.

### End-of-block rollbacks

`rollback=True` rolls back at the end of the block instead of committing, and
turns `savepoint` on, so a nested block can still fail on its own:

```python
with db.transaction(rollback=True):
    db.query(User).create(name="ada")  # rolled back when the block ends
```

A test runs against a real database this way and leaves it as it found it. The
[testing](testing.md) page builds on this.

### Databases found at call time

A decorator runs at import. If `db` is a `Database`, or a dependency-injection
proxy for one, `@db.transaction` keeps the database it got then. A test that
swaps the database later doesn't reach that function.

`transaction` from `sqlakit` finds the database each time the function runs:

```python
from sqlakit import Database, transaction

archive_db = Database("sqlite://")


@transaction  # the default database of `sqlakit.db`
def import_users() -> None: ...


@transaction(using="replica")  # an alias in `sqlakit.db`
def rebuild_report() -> None: ...


@transaction(using=lambda: archive_db)  # whatever the function returns
def archive_orders() -> None: ...
```

It takes the same arguments as `db.transaction()`. `autocommit` works the same
way. Under `asyncio`, import both from `sqlakit.asyncio`.

`@db.transaction` on `sqlakit.db` itself doesn't need this. The registry finds
its database each time a block opens.

All arguments and their defaults are listed in the
[reference](reference.md#transaction-arguments).

## autocommit() {#autocommit}

`SQLAlchemy` opens a transaction around everything, including plain reads. For
a single `SELECT`, `connect()` produces this log:

```sql
BEGIN (implicit)
SELECT 1
ROLLBACK
```

The transaction here is useless, and the `BEGIN` and `ROLLBACK` round trips
are wasted. `autocommit()` uses the connection in `AUTOCOMMIT` mode, where
every statement commits on its own:

```python
@db.autocommit
def get_dashboard(user_id: int) -> Dashboard: ...
```

The same `SELECT` now logs:

```sql
BEGIN (implicit; DBAPI should not BEGIN due to autocommit mode)
SELECT 1
ROLLBACK using DBAPI connection.rollback(); set skip_autocommit_rollback to prevent fully
```

The lines around the statement are still there, but no SQL is sent: the driver
gets no `BEGIN`, and instead of a `ROLLBACK` statement `SQLAlchemy` calls the
driver's `rollback()` as the connection goes back to the pool. On dialects
that can skip that call as well, turn it off with one argument:

```python
db = Database(url, engine_args={"skip_autocommit_rollback": True})
```

### Statements that can't run in a transaction

`VACUUM`, `CREATE DATABASE` and `CREATE INDEX CONCURRENTLY` cannot run inside
a transaction. Run them here:

```python
with db.autocommit() as conn:
    conn.execute(sa.text("VACUUM ANALYZE users"))
```

Call them outside any transaction. **Inside a transaction the block joins it**
and uses the same connection, so the statement fails with the database's own
error, such as `cannot VACUUM from within a transaction`.

### Rows kept after an exception

Every statement is already committed, so after an exception the database keeps
everything that ran before it:

```python
with db.autocommit():
    db.session.add(User(name="ada"))
    db.session.flush()  # committed
    raise RuntimeError  # `ada` stays
```

The mode is meant to work like this. Writes that have to undo together belong
in `transaction()`.

## session_factory() {#session-factory}

Binds a session for the block, and yields it:

```python
with db.session_factory() as session:
    session.add(User(name="ada"))
    session.commit()
```

Unlike the three blocks above, entering it takes no connection from the pool.
The session is created at once, and the checkout happens the first time the
session needs a connection: on a query or a flush, not on `add()`. A block
that never uses the session never touches the database. `sessionmaker()`
behaves the same way, so code written against it keeps its habits here.

Inside another block it runs on the connection already bound, like
`connect()`. It also commits nothing by itself: commit on the session, or use
`transaction()`.

## Async blocks {#async}

`sqlakit.asyncio` has the same blocks, and needs the `sqlakit[asyncio]` extra.
Opening a block is awaited, reading the context is not:

```python
async with db.transaction():
    await db.session.flush()

db.connection  # the same
db.session  # the same
db.in_transaction()  # the same
```

### Concurrent tasks

A task started inside a block inherits its context, so every coroutine under
`asyncio.gather()` runs on the same session and the same connection:

```python
async with db.transaction():
    await asyncio.gather(load_users(), load_teams())  # both on one session
```

A `SQLAlchemy` session is not built for two tasks at once. Reads under it
often pass with no error at all, which is the dangerous part, and two tasks
that overlap on a write raise `InvalidRequestError: Session is already
flushing`. Give each task a block of its own, and each gets its own
connection:

```python
async def load_users() -> list[User]:
    async with db.connect():
        return await db.query(User).all()


await asyncio.gather(load_users(), load_teams())
```

### Background tasks

`FastAPI` runs `BackgroundTasks` after the response, when the handler's block
is already closed. Accessing `db.session` there raises `MissingSessionError`:

```python
@app.post("/users")
@db.transaction
async def create_user(background: BackgroundTasks) -> UserResponse:
    background.add_task(notify)  # runs after the session is closed
    ...


async def notify() -> None:
    async with db.transaction():  # a background task needs a block of its own
        ...
```

## Worker-thread blocks

`FastAPI` runs an endpoint written as `def` in a worker thread. Open the block
on the endpoint itself, so the connection belongs to the thread that uses it:

```python
@app.get("/users")
@db.transaction  # opens and closes in the worker thread
def list_users() -> list[UserResponse]:
    return [UserResponse.model_validate(user) for user in db.query(User).all()]
```

A block opened outside that thread, in a middleware or an async dependency, is
visible inside it, because the thread starts with a copy of the context. The
endpoint would then run its statements on a connection another thread holds.
`SQLite` refuses that outright, and the other drivers do it without a word.

An `async def` endpoint has none of this to think about. It runs on the loop,
where a block covers one request and nothing else. `Flask` works like the
endpoint above: every request already has a thread of its own, and the block
goes on the view.

Next: [queries](queries.md) for what runs inside these blocks.
