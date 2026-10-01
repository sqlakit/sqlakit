# Reference

## Blocks

| | opens | commits | inside another block |
| --- | --- | --- | --- |
| `db.connect()` | a connection | no | reuses the bound connection |
| `db.transaction()` | a connection and a transaction | when the block ends | runs on the bound connection, except under `autocommit()` |
| `db.autocommit()` | a connection in `AUTOCOMMIT` | every statement | joins the transaction |
| `db.session_factory()` | a session at once, a connection on first use of the session | no | reuses the bound connection |

Under `asyncio`, `db.connection` in a `session_factory()` block raises
`MissingConnectionError` until the session has used a connection, because a
property cannot await the checkout.

## Transaction arguments {#transaction-arguments}

| | default | |
| --- | --- | --- |
| `savepoint` | `False` | Run a nested block as a savepoint. Applies to the blocks under it as well. |
| `join_nested` | `True` | Blocks under this one reuse its connection. |
| `rollback` | `False` | Roll back instead of committing. Implies `savepoint`. |
| `commit_on_error` | `None` | Exception types that commit anyway. |
| `retry_on` | `None` | Exception types or a predicate that triggers a retry. Works as a decorator only, and only on the block that owns the transaction. |
| `max_retries` | `3` | How many extra attempts `retry_on` gets. |
| `backoff` | exponential with jitter | Seconds to wait before attempt `n`, counting from zero. |

## Defaults {#defaults}

`SQLAKit` merges whatever you pass in `engine_args` and `session_args` over
these.

| | default | why |
| --- | --- | --- |
| `pool_pre_ping` | `True` | A connection dropped by the server, a proxy or a failover leads to a reconnect instead of an error mid-statement. |
| `pool_recycle` | `1800` | Reopen a connection before something else closes it: `MySQL`'s eight-hour `wait_timeout`, `PgBouncer` and cloud balancers all close idle connections. |
| `expire_on_commit` | `False` | Attributes stay readable after a commit. With expiry on, a read after a commit issues a lazy `SELECT`, which under `asyncio` fails with `MissingGreenlet`. |

`SQLAKit` doesn't set `pool_size`, `max_overflow` or `isolation_level`: they
depend on the worker count, the database's limits and the dialect.

It doesn't set `autoflush` either, so it is `SQLAlchemy`'s `True`: a query
writes the pending changes out first, without committing them.

```python
with db.transaction():
    user = User.query.get_one(1)
    user.name = "grace"
    User.query.count()  # the UPDATE goes out first, then this SELECT
```

A block that alternates changes and queries writes once per query rather than
once at the end. `session.no_autoflush` holds them back.

Turning it off is the other way round: a query no longer sees what
`db.session.add()` put in the session. `save()` is unaffected, since it
flushes on its own.

```python
db = Database(DB_URL, session_args={"autoflush": False})
```

## Async {#async}

`sqlakit.asyncio` mirrors `sqlakit`. Calls that reach the database are
awaited. Reading the context is not. Requires the `sqlakit[asyncio]` extra.

| synchronous | `asyncio` |
| --- | --- |
| `with db.connect():` | `async with db.connect():` |
| `with db.transaction():` | `async with db.transaction():` |
| `with db.autocommit():` | `async with db.autocommit():` |
| `db.dispose()` / `db.ping()` | `await db.dispose()` / `await db.ping()` |
| `user.save()` / `user.delete()` | `await user.save()` / `await user.delete()` |
| `user.merge()` / `user.refresh()` | `await user.merge()` / `await user.refresh()` |
| `User.query.get(id)` / `.get_one(id)` | `await User.query.get(id)` / `.get_one(id)` |
| `User.query.page(...)` / `.cursor_page(...)` | `await User.query.page(...)` / `.cursor_page(...)` |
| `db.sql(...).all()` | `await db.sql(...).all()` |
| `query.where(...)`, `query.order_by(...)` | the same, unawaited |
| `db.connection`, `db.session`, `db.in_transaction()` | the same |

`SQLAlchemy`'s async drivers require a running `asyncio` loop, so `trio` is not
supported.

## Database

::: sqlakit.Database
    options:
      inherited_members: true

## The registry

::: sqlakit.Databases

::: sqlakit.db

::: sqlakit.Router
    options:
      inherited_members: true

### Defaults

::: sqlakit.DEFAULT_ENGINE_ARGS

::: sqlakit.DEFAULT_SESSION_ARGS

::: sqlakit.DEFAULT_ALIAS

::: sqlakit.CASE_INSENSITIVE_COLLATIONS

## Transaction

::: sqlakit.Transaction

::: sqlakit.RetryingTransaction
    options:
      inherited_members: true

::: sqlakit.transaction

::: sqlakit.autocommit

## Model

All model behavior lives on the mixin. `Model` combines that mixin with a
plain declarative base.

::: sqlakit.orm.Model

::: sqlakit.orm.ModelMixin
    options:
      inherited_members: true

::: sqlakit.orm.SoftDeletes

## Query

::: sqlakit.orm.Query
    options:
      inherited_members: true

::: sqlakit.orm.ColumnQuery

::: sqlakit.orm.QueryDescriptor

::: sqlakit.import_models

::: sqlakit.import_string

::: sqlakit.testing.assert_queries

::: sqlakit.testing.AssertQueries

::: sqlakit.Recording

::: sqlakit.Statement

::: sqlakit.QueryStats

::: sqlakit.Page

::: sqlakit.UncountedPage

::: sqlakit.CursorPage

::: sqlakit.OrderBy

::: sqlakit.orderable_columns

## SQL templates

Covered in [SQL templates](sql.md).

::: sqlakit.sql.SQL

::: sqlakit.sql.SQLQuery
    options:
      inherited_members: true

::: sqlakit.sql.SQLRows

::: sqlakit.sql.Templates

::: sqlakit.sql.sql_macro

::: sqlakit.sql.Param

::: sqlakit.sql.Sql

::: sqlakit.sql.Context

::: sqlakit.sql.Inline

::: sqlakit.sql.tpl

## The async classes

The same classes, awaited.

::: sqlakit.asyncio.Database
    options:
      inherited_members: true

::: sqlakit.asyncio.Databases
    options:
      inherited_members: true

::: sqlakit.asyncio.db

::: sqlakit.asyncio.Transaction

::: sqlakit.asyncio.RetryingTransaction
    options:
      inherited_members: true

::: sqlakit.asyncio.transaction

::: sqlakit.asyncio.autocommit

::: sqlakit.asyncio.orm.ModelMixin
    options:
      inherited_members: true

::: sqlakit.asyncio.orm.SoftDeletes

::: sqlakit.asyncio.orm.Query
    options:
      inherited_members: true

::: sqlakit.asyncio.orm.ColumnQuery

::: sqlakit.asyncio.orm.QueryDescriptor

::: sqlakit.asyncio.sql.SQL

::: sqlakit.asyncio.sql.SQLQuery
    options:
      inherited_members: true

::: sqlakit.asyncio.sql.SQLRows

## Arguments

::: sqlakit.EngineArgs

::: sqlakit.SessionArgs

::: sqlakit.ValidationArgs


::: sqlakit.TemplatesLike

::: sqlakit.DatabaseConfig

::: sqlakit.UrlParts

## Exceptions

Every exception inherits `SQLAKitError`, and also a builtin exception that
reflects the kind of failure. The second base is convenient for mapping errors
to API responses:

| also a | what happened | typical response |
| --- | --- | --- |
| `ValueError` | invalid input: an unknown ordering field, or a cursor that does not decode | 400 |
| `TypeError` | a mistake in the code: a page with no ordering, or `get` on a narrowed query | 500 and a fix |
| `RuntimeError` | the block is in the wrong state: no connection, no session, or a transaction already rolled back | 500 |
| `KeyError` and `SQLAlchemy`'s exceptions | their usual meaning, for code that already catches them: a missing row is also a `NoResultFound` | |

```python
import sqlakit

try:
    page = User.query.order_by(sort).page(limit=20)
except sqlakit.UnknownOrderFieldError:  # invalid field in this request
    ...
except sqlakit.SQLAKitError:  # anything from this library
    ...
except ValueError:  # any bad input, from here or elsewhere
    ...
```

::: sqlakit.SQLAKitError

### Blocks

::: sqlakit.MissingConnectionError

::: sqlakit.MissingSessionError

::: sqlakit.TransactionRolledBackError

::: sqlakit.RetryNotSupportedError

::: sqlakit.DetachedInstanceError

### Configuration

::: sqlakit.DatabaseNotConfiguredError

::: sqlakit.DatabaseAlreadyConfiguredError

::: sqlakit.UnknownDatabaseError

::: sqlakit.AliasInUseError

::: sqlakit.DefaultAliasError

::: sqlakit.MissingDefaultDatabaseError

::: sqlakit.MissingRegistryError

::: sqlakit.InvalidDatabaseConfigError

::: sqlakit.MissingDatabaseUrlError

::: sqlakit.ConflictingDatabaseUrlError

::: sqlakit.UnknownImportPathError

::: sqlakit.MissingDependencyError

### Ordering and pagination

::: sqlakit.UnorderedPageError

::: sqlakit.UnknownOrderFieldError

::: sqlakit.InvalidSortStringError

::: sqlakit.ConflictingJoinError

::: sqlakit.InvalidOrderFieldError

::: sqlakit.InvalidNullsError

::: sqlakit.UncomparableOrderingError

::: sqlakit.InvalidCursorError

::: sqlakit.NullCursorValueError

::: sqlakit.PageItemsMismatchError

### Rows and queries

::: sqlakit.InstanceNotFoundError

::: sqlakit.MultipleInstancesFoundError

::: sqlakit.KeyLookupError

::: sqlakit.DuplicateKeyError

::: sqlakit.UnknownFieldError

::: sqlakit.BulkQueryError

::: sqlakit.RawStatementError

### SQL templates

::: sqlakit.SQLNotConfiguredError

::: sqlakit.TemplateNotFoundError

::: sqlakit.StrayParameterError

::: sqlakit.ParameterPathError

::: sqlakit.InlineValueError

::: sqlakit.MacroSyntaxError

::: sqlakit.UnknownMacroError

::: sqlakit.MacroArgumentError

::: sqlakit.MacroDefinitionError

::: sqlakit.UnknownIdentifierError

::: sqlakit.ProjectConfigError
