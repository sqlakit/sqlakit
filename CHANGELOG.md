# Changelog

## Unreleased

### Fixed

- `tpl.order_by` with nothing to sort by, and `ctx.no_order`, wrote
  `(SELECT NULL)` on every database, and Snowflake refuses a subquery after
  `ORDER BY`. They write `NULL` now, and `(SELECT NULL)` on PostgreSQL and
  SQL Server, which refuse a bare `NULL` there.

## 0.22.6

### Added

- `tpl.if_not_set`, another name for `tpl.unless_set`, which reads as the
  other way round from `tpl.if_set`.
  A project with a macro of its own named `if_not_set` now gets
  `MacroDefinitionError`, as two macros share the name: drop yours, or give it
  another name.

## 0.22.5

### Changed

- A problem `sqlakit check --lint` finds shows the rendered line the linter
  stopped on, with a `^` under the place, in place of the SQL the call wrote.

## 0.22.4

### Added

- `sqlakit check --lint sqruff` or `--lint sqlfluff` renders every template,
  running the project's Python macros, and fails where the linter cannot
  parse what they wrote. The problem is put on the call that wrote the SQL:
  `users/owned.sql:6:7: tpl.owned_by writes SQL sqruff cannot parse`. Each
  template renders with every parameter given and with none, on the dialect
  the database URL names, or `--dialect`.
- `sqlakit render --out DIRECTORY` writes the rendered SQL of every template
  into files, both ways.

## 0.22.3

### Added

- `sqlakit export sqlfluff` writes the `sqlfluff` settings that read the
  templates into `pyproject.toml`, as `sqlakit export sqruff` does for
  `sqruff`: the `placeholder` templater, the same five rules turned off, and a
  value for each parameter named like a word the dialect keeps, asked of the
  installed `sqlfluff`. `--check` fails when they are out of date.
- `sqlakit export sql-formatter` writes `.sql-formatter.json`, which tells
  `sql-formatter` that `:name` is a parameter, with the language of the
  project's dialect. Without it, `sql-formatter` splits `c = :limit::int` on
  PostgreSQL into `c =: limit ::int`. The other settings of the file stay.
- The documentation covers `pgFormatter`, which formats a template with no
  settings.

## 0.22.2

### Added

- The documentation covers the editors: the
  [VS Code extension](https://marketplace.visualstudio.com/items?itemName=sqlakit.sqlakit),
  `sqlakit-lsp`, the language server it runs, and how to run that server in
  PyCharm through LSP4IJ or in any other editor. See
  [editor support](https://sqlakit.readthedocs.io/en/stable/sql/#editor-support).

## 0.22.1

### Changed

- `sqlakit export sqruff` turns on every rule `sqruff` has, `rules = "all"`,
  where it writes `[tool.sqruff.core]`: a table already there stays as it is.
  It turns off `RF02` and `RF03` as well as `RF01`, `AL05` and `ST03`, which
  read a table an SQL macro takes, `tpl.paid(o)`, as a column.
- `sqlakit export sqruff` writes no comment into
  `[tool.sqruff.templater.placeholder]`, and drops the one it wrote before.
- The tests run on SQLAlchemy 2.1 as well as 2.0, the integration tests on
  every database included.

## 0.22.0

### Fixed

- `sqlakit export sqruff` asks `sqruff`, when it's installed, which parameter
  names its dialect keeps. A template on SQLite with `:exclude` or `:row` was
  unparsable to `sqruff`, since SQLAlchemy doesn't keep those words, and
  `sqruff fix` broke the lines around it.

### Removed

- `tpl.when`. Its argument was a clause, not an expression, so a template with
  it was not SQL to a linter, and `sqruff fix` broke the lines around it. Write
  the clause as a condition:

  ```sql
  -- before
  FROM orders AS o
  tpl.when(:user_name, JOIN users AS u ON u.id = o.user_id AND u.name = :user_name)

  -- after
  FROM orders AS o
  WHERE tpl.if_set(
      :user_name,
      EXISTS (SELECT 1 FROM users AS u WHERE u.id = o.user_id AND u.name = :user_name)
  )
  ```

## 0.21.0

### Added

- Built-in macros: `if_set`, `unless_set`, `when`, `between`, `order_by`,
  `icontains`, `icollate`, `identifier`, `each`, `in_list`, `array`,
  `arrays_overlap`, `array_contains_all`, `values`, `json_object`,
  `array_agg`, `string_agg`, `array_contains`, `on_dialect` and `include`.
- `@sql_macro` for macros of your own, registered with
  `Templates(macros=[...])`, as objects or by import path. `Param`, `Sql`,
  `Context` and `tpl`, which calls a built-in macro from them, are public in
  `sqlakit.sql`. A macro that is a piece of SQL is written in a `.sql` file
  instead, as `SELECT <expression> AS <name> FROM <arguments>;`, and a file
  whose name ends in `macros.sql` is found in the template directories.
  `@sql_macro("file.sql")` keeps a macro's SQL in a file, and its function
  returns the values the SQL reads.
- The `sqlakit` command is back, with three subcommands. `sqlakit macros` lists
  the macros, and `sqlakit check` checks every template of a project and exits
  1 for a problem and 2 for a project it can't read. Both read where the
  templates and the macros are from the project's code without running it.
- `sqlakit export sqruff` writes the `sqruff` settings that read the templates
  as SQL into `pyproject.toml`: the `placeholder` templater, and a value for
  each parameter named like a keyword, such as `:limit`.
- `sqlakit export pycharm` writes `.idea/sqlakit.sql`, which declares the
  macros as functions of the `tpl` schema for a DDL data source, and sets the
  dialect of the template directories in `.idea/sqldialects.xml`, so PyCharm
  and DataGrip read a `tpl.` call as a known function.
- `[tool.sqlakit.templates]` in `pyproject.toml` takes `paths`, `macros`,
  `namespace` and `dialect`, for a project whose code builds its paths in a way
  `sqlakit check` and `export` can't read.
- `Inline` writes a value into the SQL where SQL takes no bound one: a stage
  in `COPY INTO`, a table being created, a sample's size. `Inline.stage` and
  `Inline.name` check what they are given, and a value is written only in those
  places.
- `IN (:ids)` binds a list as `IN :ids` does, and a `LIMIT :limit` or
  `OFFSET :offset` with no value takes every row and skips none, on every
  database.
- Exceptions for templates and macros, each also a `ValueError` or a
  `TypeError`: `MacroSyntaxError`, `UnknownMacroError`, `MacroArgumentError`,
  `MacroDefinitionError`, `ParameterPathError`, `InlineValueError`,
  `UnknownIdentifierError`, `InvalidSortStringError` and `ProjectConfigError`.
  `db.sql.check()` raises them in place of Jinja's `TemplateSyntaxError`.
- `Templates(namespace="q")` names the schema macro calls are written under,
  for a database with a real schema named `tpl`.
- `sqlakit_models = app` in the pytest settings imports every `models` module
  under the package before the plugin creates the tables, so a run of a few
  tests has them all.

### Changed

- SQL templates are SQL: `:name` parameters and `tpl.` macro calls in place of
  `Jinja`, so a formatter and a linter read a template without a context.
  Every `.sql` file under `templates=` and every `db.sql.from_string(...)`
  reads this syntax.

  ```sql
  -- before
  SELECT * FROM users
  WHERE team IN {{ teams }}
  {% if search %} AND name ILIKE {{ '%' ~ search ~ '%' }} {% endif %}
  ORDER BY {{ column | identifier }}

  -- after
  SELECT * FROM users
  WHERE team IN (:teams)
    AND tpl.if_set(:search, tpl.icontains(name, :search))
  ORDER BY tpl.identifier(:column, id, name)
  ```

  `{{ x.y }}` is `:x.y`, `| inclause` is `IN (:x)` or `tpl.each(:x)`, a
  `{% include %}` of a whole query is `tpl.include('q.sql')`, and a filter or a
  global is an `@sql_macro` function. The
  [`migrate-from-jinja`](https://github.com/sqlakit/sqlakit/tree/main/.claude/skills/migrate-from-jinja)
  skill has the whole mapping.
- A parameter binds under the name the template gives it: `:since` in place of
  `:since__1`. Code that reads the bound names, such as a test of the compiled
  SQL, sees the new ones.
- `Templates(...)` registers its macros when it is built, so a macro that
  can't be registered raises there and not on the first query.
- `StrayParameterError` asks for the value by keyword, `name=...`.

### Fixed

- `Query.order_by` read the nulls of a sort string only in snake case:
  `score.asc.nullsFirst` is now `score.asc.nulls_first`, as it should be.

### Removed

- `Jinja` templates, the `sql` extra and the `jinja2sql` dependency. Install
  `sqlakit` in place of `sqlakit[sql]`. `Templates` takes no `filters=` or
  `globals=`, and `sqlakit.sql.Filter` and `AsyncFilterError` are gone. A
  filter or a global is a macro now:

  ```python
  # before
  Templates(BASE_DIR, filters={"upper": lambda value: value.upper()})

  # after
  from sqlakit.sql import Param, sql_macro


  @sql_macro
  def upper(value: Param) -> str:
      return f"UPPER({value})"


  Templates(BASE_DIR, macros=[upper])
  ```

## 0.20.0

### Changed

- The debug server moved to a package of its own,
  [`sqlakit-debugserver`](https://github.com/sqlakit/sqlakit-debugserver), and
  runs as `sqlakit-debugserver` in place of `sqlakit debugserver`. Install it
  with `pip install sqlakit-debugserver`. `--sqlakit-report` needs it too,
  since the report is the server's page.
- `db.recording(send_to=...)` takes a function of the recording, called when
  the block ends, in place of `debugserver=`. `DebugServer` moved to the new
  package and is such a function:

  ```python
  # before
  from sqlakit import DebugServer

  with db.recording("GET /users", debugserver=DebugServer("localhost", 5555)):
      ...

  # after
  from sqlakit_debugserver import DebugServer

  with db.recording("GET /users", send_to=DebugServer("localhost", 5555)):
      ...
  ```

  The shorthand `debugserver=("localhost", 5555)` is gone.

### Removed

- The `sqlakit` command. Its one subcommand, `debugserver`, is
  `sqlakit-debugserver` now.
- `sqlakit.DebugServer`, now `sqlakit_debugserver.DebugServer`.

## 0.19.0

### Added

- `in_bulk()` on a query returns the matching rows as a dict. With no argument
  the key is the primary key, a column keys by its value, and several columns
  by the tuple of theirs, with the key's type carried through:
  `User.query.in_bulk(User.email)` is a `dict[str, User]`. A key column the
  query defers is loaded with the row, a key that is not a column of the model
  raises `UnknownFieldError`, and two rows under one key raise
  `DuplicateKeyError` rather than losing a row.
- A [comparison page](https://sqlakit.readthedocs.io/en/stable/comparison/):
  connection, session and transaction management, each block next to the
  `SQLAlchemy` code it stands in for.

### Fixed

- `with_expression()` reads an instance the session already holds again, so
  the attribute takes this read's expression. Before, a query method called
  twice with different parameters in one session left the first value on the
  instances the second read returned.

## 0.18.0

### Changed

- `Model.query` on a model that assigns no query of its own reads as a query
  of that model: `User.query.get(1)` is a `User | None` and `User.query.all()`
  a `Sequence[User]`, where both were `Any`. A checker now reports an attribute
  the model does not have on a row read through `Model.query`, so a project
  that type-checks may see errors the `Any` was hiding. `as_descriptor()` and
  `QueryDescriptor(MyQuery)` read as they did.

## 0.17.0

### Added

- `refresh()` takes the attributes to read again as the model declares them:
  `user.refresh(User.team)` rather than `refresh(attribute_names=["team"])`.
  Naming a relationship is how a `lazy="raise"` one is read after a refresh,
  and a name still goes in as a string or as a list.
- `refresh(with_relationships=True)` reads every relationship the model
  declares, loaded or not, for a test that compares a whole instance and would
  otherwise name them one by one. The instances behind those relationships are
  expired as well, since a relationship read again hands back what the session
  holds, with the values it was loaded with. It costs a statement per
  relationship.

## 0.16.0

### Added

- `load_only()`, `defer()`, `undefer()`, `undefer_group()` and
  `with_expression()` on a query, the loader options for columns beside the
  ones for relationships. A column left out is read when something touches it,
  one statement per instance, so it pays off on a wide column a page does not
  show.
- `filter_by()` takes a mapping beside its keywords, as `create()` and the
  template calls do, so a filter held as a dict goes in as it is:
  `filter_by(request.query_params)`.

### Documentation

- The SQL templates page says what each call in a read is for, which is why
  there are three: the template's values, pydantic's arguments, and how many
  rows.
- The testing page reads in one pass. Several databases were covered twice, in
  two places, and the hand-written fixtures for them now follow the
  hand-written ones they build on.

## 0.15.0

### Added

- A template's values go in as a mapping as well as by keyword, positionally or
  as `context=`, so a caller holding a dict passes it rather than unpacking it
  at every call site. A keyword beside a mapping replaces the value of that
  name, the mapping is read rather than changed, and a value named `context`
  is one of the mapping's own. `db.sql(...)`, `from_file`, `from_string` and a
  query's `from_sql` take it.
- `create()` takes a mapping beside its keywords, and `update()` takes keywords
  beside its mapping: `create(payload)` and `update(team="green")` write what
  `create(**payload)` and `update({"team": "green"})` wrote.

## 0.14.0

### Added

- `sqlakit_db` returns several databases: a dict naming each, which is what a
  marker picks by, or a list, where it picks by the name each database carries.
  A project that builds its databases itself and pins its models with
  `set_db()` hands them over as they are, rather than registering them in a
  `Databases()` made for the fixture, which renamed them.
- `assert_queries` as a fixture, watching whatever `sqlakit_db` returned, so a
  project with no registry counts the queries of the database it names.
  `sqlakit.testing.AssertQueries` is the type a test annotates it with.
- `db.using()` on a registry takes the database itself, as
  `recording(using=...)` and a query's `using()` already did. A database the
  registry does not hold raises `UnregisteredDatabaseError` instead of saying
  it is not configured.

### Fixed

- A savepoint a nested block takes is taken through the session that owns the
  savepoints of that connection, so the two are released in the order they were
  taken. A session committing inside such a block released the older savepoint,
  and the database ended the block's with it: the block then failed on
  `savepoint ... does not exist`, at its end or at the teardown after it. A
  block whose savepoint that session has already released ends quietly, since
  the commit kept its work.

### Changed

- A block binds its context without a generator, and a nested block, which
  every call under a decorated function opens, takes 3.5 µs where it took 4.6.

## 0.13.0

### Added

- `Filter(func, bind=True)` registers a template filter that writes SQL of its
  own and binds the values inside it, where a plain function returns one value
  and has it bound. The filter is called with a `jinja2sql` `Binder`, so one
  written for `jinja2sql` works here as it is. The `sql` extra now needs
  `jinja2sql>=0.12.0`, which is where the binder arrived.
- `typed()` takes the keywords `pydantic` takes: `context` for a validator that
  reads one, and `strict`, `by_alias` and the rest beside it. They reach every
  row the query reads, a batch of `chunks` included, and `ValidationArgs` lists
  them.

### Fixed

- A test written `async def` over a synchronous database rolls back as a
  synchronous one does. The plugin chose the transaction fixture by whether the
  test was a coroutine and then awaited every block, so a test that awaits
  something other than the database, a handler it runs in a worker thread among
  them, failed at setup with `TypeError: ... does not support the asynchronous
  context manager protocol`. A `def` test on an async database says to write it
  as `async def`, rather than failing on the same protocol from the other side.

### Documentation

- Every page leads with the reader's case and the line to type, and explains
  the mechanism after the example: the database page, getting started, queries,
  the worker-thread section on the context page, and the sections introducing
  `set_loaded()`, `import_models()` and `sql.check()`.

## 0.12.0

### Added

- `db.unbound()` hides the block around it, so code that reaches for `session`
  without opening a block raises `MissingSessionError` instead of borrowing
  the block a test opened. A block opened inside still joins the transaction
  around it and rolls back with it. On a registry it covers every database.
- `sqlakit_unbound = true` in the ini file hides the block for a whole suite,
  and `@pytest.mark.db(unbound=True)` or `unbound=False` sets it for one test.
  Both default to the previous behaviour, which lends the test's block to the
  code it calls.

## 0.11.1

### Changed

- `EngineArgs` and `SessionArgs` take a keyword they do not list, as
  `create_engine` and `sessionmaker` do: `executemany_mode` on `psycopg2`,
  `prepared_statement_cache_size` on `asyncpg`. A wrong value for a keyword
  they do list is still an error. `typing-extensions>=4.13` is declared for
  this, having arrived through `sqlalchemy` until now.

## 0.11.0

### Added

- `Database.aliases` and `db[name]`. A database carries one name and returns
  itself for it, as a registry returns the databases it holds, so code that
  takes either does not have to ask which it was given.
  `@pytest.mark.db(using="default")` works for a project whose `sqlakit_db`
  returns a database rather than a registry.

### Changed

- A registry handed its default by `register("default", db)` proxies to that
  database. `session`, `transaction()`, `connect()`, `engine`, `ping()` and the
  rest reach it, where they raised `DatabaseNotConfiguredError` before, which is
  what a project registering its only database ran into. A registry `configure()`
  built is unchanged.
- `using()` redirects a model pinned with `set_db(db)` when that database is the
  registry's default, as it already did for a model on the default alias.

### Documentation

- The testing page has the conftest for a project with a base for each
  database: the registry under `sqlakit_db`, and both schemas under
  `sqlakit_schema`.
- The plugin's own docstring shows `sqlakit_base`, the fixture a project with
  models defines. It showed `sqlakit_db` and `sqlakit_metadata`, which are the
  overrides for a project without one.
- The routing page says what reaches a registered default, and the message a
  registry raises is about the settings that stay on the database it holds.

## 0.10.9

### Fixed

- A registry answers a dunder before it reads any state of its own. A container
  that resolves a `Database` patches `__getattribute__` on the class and probes
  every attribute for a marker of its own, which fell into `__getattr__`, read
  `self.__dict__`, and was caught by the patch again: the two recursed until the
  stack ran out, and every query through the model layer reached it.
- The `pytest` plugin rolls back a registry holding one database through
  `transactions()`. It called `transaction()` on the registry itself, which has
  a database of its own only when `configure` built it, so a project that
  registered its single database failed at setup.

## 0.10.8

### Documentation

- Every heading on a concept or a reference section is a noun phrase: `Row
  reads`, `Template syntax`, `Table iteration`, `Async blocks`. The steps of the
  tutorial keep their verbs and the questions on the routing page keep their
  question marks.
- Soft deletes and the sections on what a feature does not do are named the same
  way on every page that has them. Links and anchors follow the renames.

## 0.10.7

### Changed

- The filters on the debug server's page are an input for each field: the
  application, the database, the tags, the kind of statement and the table.
  Each carries what is picked, and types down to what is asked for.
- Two terms of one field are read as either of them, so a second table widens
  the list rather than emptying it. Terms of different fields are read as both.

## 0.10.6

### Changed

- The width chosen on the debug server's page is the width a statement laid out
  by clauses wraps at. It reached the formatter alone, and the clauses broke at
  a hundred characters however it was set.
- A part still too long has its brackets opened, so an `IN` list of fifty ids
  reads as fifty lines rather than as one.

## 0.10.5

### Changed

- A clause over 100 characters is broken at the commas and the `AND`s that are
  its own, so a report written by hand reads as lines rather than as a
  paragraph. Brackets and quoted values are left alone, and a statement a
  mapper wrote is unchanged.

## 0.10.4

### Fixed

- A statement whose template names itself in a `/* ... */` comment keeps that
  comment on a line of its own, rather than on the line of the first clause.
- A statement opening with a `--` comment is counted as the kind of statement
  it runs. It counted as `other`, since only a block comment was read past.

### Changed

- The tables in the filters are listed under the database they live on. The
  database was a tag beside the name, in the same type, so a long name was cut
  and the two ran together.

## 0.10.3

### Fixed

- A statement whose template names itself in a `--` comment is laid out on the
  debug server's page again. Both layouts collapsed the statement to one line
  first, which left the rest of it inside the comment.

### Changed

- The filters on that page are wide enough for the database beside a table.

## 0.10.2

### Changed

- The debug server's page lists the databases it has seen as a filter, and
  picking one narrows the list to `db:warehouse`. Each table in the filters
  carries the databases it was touched on.

### Fixed

- A sender that names no application is named after the module the program was
  started as. `python -m myapp` sent recordings under `__main__.py`, the file
  the interpreter ran, and now sends them under `myapp`.

## 0.10.1

### Fixed

- A recording reaches a debug server once. Two blocks writing into one
  recording, which is how databases outside a registry report under a single
  label, each sent it when they ended, and the page showed the same recording
  twice. A recording now carries an `id`, and a server keeps the first that
  arrives under it.

### Documentation

- The debugging page shows how two databases outside a registry report under
  one label.

## 0.10.0

### Added

- `Database(url, alias="warehouse")` names a database built by hand. The name
  is what a recorded statement carries, so two databases in one recording, or
  two recordings under one label, can be told apart. A registry still names the
  databases it holds after the aliases they are registered under.
- `db.recording(using=...)` on a registry records only the databases named, by
  alias or in person, as `assert_queries` already took them.

### Changed

- The debug server's page names the databases a recording covered: on the row,
  beside the label of the open recording, beside each statement, and beside
  each table in the filters. A page with one database says nothing about
  databases at all.
- A row on that page says how many of its statements were slow, and its time is
  amber over 10 ms and red over 100 ms. Slow statements were marked only inside
  the recording that was open.

### Fixed

- The page finds the server again after it is restarted. An error on the stream
  set the page to disconnected and nothing cleared it, so a server that came
  back was never picked up.

## 0.9.1

### Fixed

- A registry answers for a database registered as `default`. Registering one
  used to raise, and `Model.register_db(db)` without an alias pointed the
  models at the database while leaving the registry without one, so the two
  disagreed: `db["default"]` was the registry itself, `transactions()` failed
  on it with an `AttributeError`, and the `pytest` plugin created the tables of
  one alias out of several and rolled back one database out of several.
- A registry with no database of its own raises `DatabaseNotConfiguredError`
  rather than failing on state it never built, and says to reach the default as
  `db["default"]` when that is where it was registered.

## 0.9.0

### Added

- A debug server, for seeing what a block ran while it is running.
  `sqlakit debugserver` serves a page, and
  `db.recording("GET /users", debugserver=("localhost", 5555))` sends the
  recording to it when the block ends. Sending happens on a thread of its own,
  so the block that recorded is not waiting on it, and a server that is not
  there is not the application's problem.
- The page shows the recordings as they arrive, and for the one open, every
  statement in order: what it took, its parameters, which of them ran more than
  once, and the lines of yours that ran it. `table:users ms:>50` reads as a
  search, over the label, the SQL, the tables, the database and the trace, and
  over what a recording counted. It filters by application and tag, sorts, and
  lays a statement out in full or as it was sent.
- `pytest --sqlakit-report` writes that same page for a test run, as one file
  that opens without a server. Every recording is labelled by the test and
  carries the file it lives in. `--sqlakit-report=PATH` names the file, and the
  flag on its own names it after the clock, so a run keeps the one before it.
- `sqlakit_skip_queries_from` names the files whose queries stay out of a
  report, a factory or a helper among them, so what it shows is the code under
  test rather than the rows a fixture wrote.
- `recording(..., skip_queries_from=...)`, the same for one block.
- `DebugServer` says where recordings go, and under which application and tags,
  for a project that sends from a web process and a worker at once.
- `Statement.dialect`, what ran the statement, as SQLAlchemy names it.

### Changed

- The frames a statement remembers with `stacks=True` are the ones you wrote.
  Installed packages go, the test runner and the event loop among them, and so
  does the code SQLAlchemy generates, whose line numbers lead nowhere. A caller
  with no frames of its own, a library calling from `site-packages`, gets them
  back rather than nothing.

### Documentation

- The debugging page has a section on the debug server: what the command
  prints, what to send it, and the page itself in a screenshot.
- The site carries the library's mark and colours rather than the theme's
  defaults.

## 0.8.1

### Fixed

- The `pytest` plugin opens the test's transaction before the fixtures the test
  asked for. It opened it after them, so a fixture that wrote rows wrote them
  outside the transaction: with the model layer that raised
  `MissingSessionError`, and a fixture that opened a session of its own left
  rows behind for the tests that followed. A fixture of a wider scope, one that
  seeds a module or a class, still wraps the test.

### Documentation

- The hand-written conftest inserts the fixture rather than appending it, and
  says what the order means.
- A project with no model layer has a section of its own, on `sqlakit_db` and
  `sqlakit_metadata`.

## 0.8.0

### Added

- A `pytest` plugin, installed with the library and turned on with
  `sqlakit = true`. It registers the `db` marker, creates the schema once for
  the session, and runs every marked test in a transaction that rolls back.
  Tests without the marker connect to nothing.
- `@pytest.mark.db(using=...)` names the databases a test opens, by alias or by
  database. Without it a test opens every one, which for most projects is the
  one they have.
- Five fixtures a project overrides: `sqlakit_base`, the base its models
  inherit, which is also where the database comes from; `sqlakit_db`, the
  database itself; `sqlakit_schema`, how the schema is created, for a suite
  running migrations against a server of its own; `sqlakit_seed`, the rows
  every test starts from; and `sqlakit_metadata`, for a project with no model
  layer.

### Documentation

- The testing page opens with the plugin, and keeps the hand-written conftest
  after it. It says what `autoflush` costs, when `save()` is needed, and how to
  start a server with `pytest-docker`.

## 0.7.4

### Documentation

- The testing page uses one conftest shape for the synchronous and the
  `asyncio` sides, which differ only in `with` against `async with`. The
  fixtures are named for what they give, `_db_schema` and `_db_transaction`,
  and one copy of the old `_db_marker` had lost its marker check, so it opened
  a transaction for every test in the suite.

## 0.7.3

### Changed

- A plain column of another table in `__orderable__` is joined through a
  relationship of the model that reaches that table, condition and all. It was
  joined on the foreign key between the tables, which a view carrying no key
  does not have, and which cannot carry a discriminator. The key is still used
  when no relationship reaches the table.

### Documentation

- The reference says that `autoflush` stays at `SQLAlchemy`'s `True`, what a
  block that alternates changes and queries pays for it, and what turning it
  off costs.
- The models page says when `save()` is needed: a new instance needs it, a row
  the block read does not, and in a loop that changes rows it is a write per
  row.

## 0.7.2

### Fixed

- A template value may be named `template` or `source`. Every keyword is a
  value, and those two collided with the argument holding the file name, which
  is positional now on `sql()`, `from_file`, `from_string` and `from_sql`.

## 0.7.1

### Fixed

- Two ordering fields that join one table on different conditions raise
  `ConflictingJoinError`. A statement joins a table once, so the second
  condition was dropped and the field ordered by the first one's rows. Give
  each field an alias, and each gets a join of its own.

## 0.7.0

### Changed

- Ordering by a field in another table joins it with an outer join. It was an
  inner one, so a row with nothing on the other side disappeared from the
  results and `page.total` counted it out. `OrderBy(..., outer=False)` keeps
  the inner join, and `nulls` says where the rows with no match go.
- A model that looks an alias up nowhere raises `MissingRegistryError`, naming
  the model and the alias. It raised `MissingDefaultDatabaseError`, whose
  message is about a configuration mapping needing a `default` key.

### Added

- A plain column of another table can be named in `__orderable__` directly,
  without an `OrderBy`. Its table is joined on the key between them, once
  however many fields name it. Naming one used to build a statement whose
  `FROM` lacked the table.
- `register_db(db)` without an alias points the model at that database, the
  same as `set_db(db)`.

## 0.6.0

### Changed

- `Page.total` is typed by how the page was read. `page(limit=20)` returns a
  `Page[User]` whose `total` is an `int`, and `page(total=False)` returns a
  `Page[User, None]`. Nothing changes at run time, but a type checker now
  refuses a page read without counting where a counted one is expected, and
  one without PEP 696 type parameter defaults refuses the library itself.
  `mypy` has them from 1.12.

### Added

- `UncountedPage[User]`, the name for a page read with `total=False`.
- `orderable_columns(model)`, every mapped column of a model. What an
  `__orderable__` that adds to the columns rather than replacing them starts
  from, since calling `orderable` there reads the method that is running.

## 0.5.1

### Added

- `Model.register_db(db, alias=...)` puts a database under an alias in a
  registry belonging to that class, so a set of models can have several
  databases without configuring the importable registry. `Model.dbs` reaches
  it, and a model under that class registers into the same one.
- `Databases.register(alias, db)` does the same on a registry directly, for a
  shard that only exists once the application runs. The alias has to be free,
  `AliasInUseError` otherwise, and cannot be `default`, `DefaultAliasError`.

### Documentation

- `__dbs__`, where a model looks an alias up, is written down, along with the
  registry of its own that `register_db` builds.
- `using()` says that a model living somewhere else still needs a block open on
  its own database. The page said both halves, two sections apart.

## 0.5.0

### Changed

- `order_by` now matches a field name whichever case convention it arrives in,
  so an API sending `userName` orders by the `user_name` the model declares. A
  model that offers both spellings is matched exactly, and a name that could
  mean either is refused.
- A name in `ignore_case` that the model does not offer now raises
  `UnknownOrderFieldError`. It used to be ignored, which left the column
  comparing with regard to case and said nothing.

### Added

- `order_by` takes `nulls`, `first` or `last`, saying where the rows with no
  value go. Without it the database decides, and `PostgreSQL` is the mirror of
  `SQLite` and `MySQL`. It fills in only what neither the sort string nor the
  model said.
- `TemplatesLike` is a public type, in `sqlakit.types` beside `EngineArgs` and
  `SessionArgs`. It names what `Database(templates=...)` takes.
- `InvalidNullsError`, raised when `nulls` is neither `first` nor `last`.

### Fixed

- Ordering with `nulls_first` or `nulls_last` no longer raises a syntax error
  on `MySQL` and `MariaDB`, which have neither. The placement is compiled for
  the dialect that runs the query, so all of them put the rows in the same
  places.

Every released version, and what changed in it. The same text is on the
[releases page](https://github.com/sqlakit/sqlakit/releases).

`SQLAKit` is on `0.x`, so a minor version is where something breaks and a patch
version is where nothing does.

## 0.4.0 (2026-08-31)

### Changed

- `order_by` takes `ignore_case` in place of `ci_fields`, which named the field
  twice. Pass `True` for the fields of the call, or the names it applies to
  when the sort arrived as a list from a request.
- Ordering without regard to case asks the database how to compare, instead of
  always ordering by `lower()`. `SQLite` orders by `COLLATE NOCASE`, and a
  dialect with no collation named for it orders by `lower()`, which every SQL
  database has. The dialect is read when the query runs, so the same model
  orders on `SQLite` under test and on the server it ships to.

### Added

- `CASE_INSENSITIVE_COLLATIONS`, the collation `ignore_case` orders by, per
  dialect. Name one to order by an index, or to decide what happens to accents.

## 0.3.1 (2026-08-29)

### Changed

- The `README` shows Active Record after the registry and multiple databases,
  with a smaller example.

## 0.3.0 (2026-08-27)

### Changed

- The async API needs the `sqlakit[asyncio]` extra, which brings
  `sqlalchemy[asyncio]` with it.

## 0.2.0 (2026-08-27)

### Changed

- `session_factory()` takes a connection from the pool when the session first
  needs one, not when the block starts, the way `sessionmaker()` works.
- A `transaction()` inside `connect()` or `session_factory()` runs on the
  connection that block already opened, instead of taking a second one.
  `transaction(join_nested=False)` and a block inside `autocommit()` still get
  their own connection.
- The minimum `SQLAlchemy` version is 2.0.22, down from 2.0.43.

### Fixed

- `cursor_page()` builds the ordering once per page instead of three times.

## 0.1.0 (2026-08-26)

First release.
