"""SQL templates: `:name` parameters and `tpl.` macros."""

import enum
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import default

from sqlakit import (
    Database,
    InlineValueError,
    InvalidSortStringError,
    MacroArgumentError,
    MacroDefinitionError,
    MacroSyntaxError,
    ParameterPathError,
    StrayParameterError,
    UnknownIdentifierError,
    UnknownImportPathError,
    UnknownMacroError,
    UnknownOrderFieldError,
)
from sqlakit import _sql as sql_module
from sqlakit._sql import Macro
from sqlakit.sql import Context, Inline, Param, Sql, Templates, sql_macro, tpl

TEMPLATES = {
    "users/list.sql": """
        SELECT name FROM users
        WHERE
            team IN :teams
            AND tpl.if_set(:search, tpl.icontains(name, :search))
        ORDER BY tpl.order_by(:order_by, id, name, team, 'nulls_last')
        LIMIT :limit
    """,
    "users/for_teams.sql": """
        SELECT name FROM users WHERE tpl.for_teams(:teams) ORDER BY id
    """,
    "users/in_teams.sql": """
        SELECT name FROM users WHERE team IN (tpl.each(:teams)) ORDER BY id
    """,
    "users/search.sql": """
        SELECT name FROM users WHERE tpl.search(:q, name, team) ORDER BY id
    """,
    "users/blue_or.sql": """
        SELECT name FROM users WHERE tpl.blue_or(:teams) ORDER BY id
    """,
    "users/bluish.sql": """
        SELECT name FROM users WHERE tpl.bluish() ORDER BY id
    """,
}


@sql_macro
def for_teams(teams: Param) -> str:
    """Rows of any of the teams, or none when no team is given."""
    return f"team IN {teams}" if teams.value else "FALSE"


@sql_macro
def search(q: Param, *columns: Sql) -> str:
    """Rows where any of the columns holds the text, regardless of case."""
    if not q.value:
        return "TRUE"
    return " OR ".join(tpl.icontains(column, q) for column in columns)


@sql_macro
def blue_or(ctx: Context, teams: Param) -> str:
    """Rows of the blue team, or of any of the teams."""
    return f"team IN {ctx.bind(['blue'])} OR team IN {teams}"


@sql_macro
def bluish() -> str:
    """Rows of the blue team, from a table of one row written out in the query."""
    return f"team IN (SELECT column1 FROM {tpl.values(['blue'])} AS v)"  # noqa: S608


def write(root: Path, templates: dict[str, str]) -> Path:
    for name, source in templates.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    templates = Templates(
        write(tmp_path, TEMPLATES),
        macros=[for_teams, search, blue_or, bluish],
    )
    db = Database(
        "sqlite://", engine_args={"poolclass": sa.StaticPool}, templates=templates
    )
    with db.transaction() as conn:
        conn.execute(sa.text("CREATE TABLE users (id int, name text, team text)"))
        for index, (name, team) in enumerate(
            [("Ann", "red"), ("bob", "blue"), ("Cid", "red"), ("dan_x", "red")]
        ):
            conn.execute(
                sa.text("INSERT INTO users VALUES (:id, :name, :team)"),
                {"id": index + 1, "name": name, "team": team},
            )
    yield db
    db.dispose()


def names(db: Database, template: str, **values: Any) -> list[str]:
    with db.connect():
        return list(db.sql(template, **values).scalars().all())


def render(source: str, dialect: sa.Dialect, **values: object) -> str:
    """Return what a template becomes on a dialect, parameters left as written."""
    template = sql_module.MacroTemplate(
        "inline.sql",
        source,
        sql_module.registered([for_teams, search, blue_or, bluish]),
    )
    ctx = Context(dialect.name, dialect.identifier_preparer, values)
    return " ".join(template.render(ctx).split())


LIST: dict[str, Any] = {
    "teams": ["red", "blue"],
    "search": None,
    "order_by": None,
    "limit": 10,
}


def test_a_template_runs_with_nothing_optional(db: Database) -> None:
    assert names(db, "users/list.sql", **LIST) == ["Ann", "bob", "Cid", "dan_x"]


def test_if_set_keeps_the_condition_when_the_value_is_there(db: Database) -> None:
    values = LIST | {"search": "AN"}
    assert names(db, "users/list.sql", **values) == ["Ann", "dan_x"]


def test_icontains_matches_a_percent_or_underscore_only_as_itself(
    db: Database,
) -> None:
    assert names(db, "users/list.sql", **LIST | {"search": "_"}) == ["dan_x"]
    assert names(db, "users/list.sql", **LIST | {"search": "%"}) == []


def test_order_by_orders_by_sort_strings(db: Database) -> None:
    values = LIST | {"order_by": ["team.desc", "name.desc"]}
    assert names(db, "users/list.sql", **values) == ["dan_x", "Cid", "Ann", "bob"]


def test_order_by_folds_the_case_convention_of_a_request(db: Database) -> None:
    values = LIST | {"order_by": "Name"}
    assert names(db, "users/list.sql", **values) == ["Ann", "Cid", "bob", "dan_x"]


def test_order_by_refuses_a_field_it_was_not_given(db: Database) -> None:
    with pytest.raises(UnknownOrderFieldError, match="password"):
        names(db, "users/list.sql", **LIST | {"order_by": "password"})


@pytest.mark.parametrize("sort", ["id.sideways", "id.asc.nulls_middle", "id.asc.x.y"])
def test_order_by_refuses_what_is_not_a_sort_string(sort: str) -> None:
    with pytest.raises(InvalidSortStringError) as raised:
        render("ORDER BY tpl.order_by(:o, id)", postgresql.dialect(), o=sort)
    assert str(raised.value).startswith(f"`{sort}` is not a sort string")


@pytest.mark.parametrize(
    "sort",
    [
        "teamId.asc.nulls_first",
        "teamId.ASC.nullsFirst",
        "team_id.asc.NULLS_FIRST",
    ],
)
def test_order_by_reads_a_sort_string_in_any_case_convention(sort: str) -> None:
    source = "ORDER BY tpl.order_by(:o, name, team_id)"
    assert render(source, postgresql.dialect(), o=[sort]) == (
        "ORDER BY team_id ASC NULLS FIRST"
    )


def test_order_by_needs_the_columns_it_may_sort_by() -> None:
    with pytest.raises(MacroArgumentError, match="takes at least 2 arguments"):
        render("ORDER BY tpl.order_by(:o)", postgresql.dialect(), o="id")


def test_order_by_sorts_by_an_expression_under_a_name() -> None:
    source = "ORDER BY tpl.order_by(:o, u.id, name = name COLLATE 'und-ci-ai')"
    assert render(source, postgresql.dialect(), o=["name.desc", "id"]) == (
        "ORDER BY name COLLATE 'und-ci-ai' DESC, u.id ASC"
    )


def test_order_by_places_nulls_where_the_template_says_unless_asked() -> None:
    source = "ORDER BY tpl.order_by(:o, id, name, 'nulls_last')"
    assert render(source, postgresql.dialect(), o=["name.asc.nulls_first", "id"]) == (
        "ORDER BY name ASC NULLS FIRST, id ASC NULLS LAST"
    )


def test_order_by_places_nulls_on_mysql_without_nulls_last() -> None:
    source = "ORDER BY tpl.order_by(:o, id, 'nulls_last')"
    assert render(source, mysql.dialect(), o="id.desc") == (
        "ORDER BY id IS NULL ASC, id DESC"
    )


def test_order_by_refuses_nulls_after_the_call() -> None:
    with pytest.raises(
        MacroArgumentError, match="pass 'nulls_last' or 'nulls_first' as an argument"
    ):
        render(
            "ORDER BY tpl.order_by(:o, id)\n  nulls last", postgresql.dialect(), o=None
        )


def test_order_by_orders_by_nothing_when_nothing_is_asked() -> None:
    assert render("ORDER BY tpl.order_by(:o, id)", postgresql.dialect(), o=[]) == (
        "ORDER BY (SELECT NULL)"
    )


def test_limit_and_list_parameters_are_bound(db: Database) -> None:
    values = LIST | {"teams": ["blue"], "limit": 1}
    assert names(db, "users/list.sql", **values) == ["bob"]


def test_an_application_macro_takes_a_parameter(db: Database) -> None:
    assert names(db, "users/for_teams.sql", teams=["blue"]) == ["bob"]
    assert names(db, "users/for_teams.sql", teams=[]) == []


def test_a_macro_writes_sql_for_the_dialect() -> None:
    source = "SELECT tpl.json_object('a', 1, 'b', tpl.if_set(:x, 2, 3))"
    assert render(source, postgresql.dialect(), x=1) == (
        "SELECT JSON_BUILD_OBJECT('a', 1, 'b', 2)"
    )


def test_icontains_is_ilike_on_postgres() -> None:
    sql = render("WHERE tpl.icontains(u.name, :q)", postgresql.dialect(), q="a")
    assert sql == "WHERE u.name ILIKE :q__like__1 ESCAPE '!'"


def test_the_layout_of_a_template_survives_rendering(db: Database) -> None:
    with db.connect():
        statement = db.sql("users/list.sql", **LIST).statement
    assert str(statement) == (
        "/* users/list.sql */\n"
        "\n"
        "        SELECT name FROM users\n"
        "        WHERE\n"
        "            team IN (__[POSTCOMPILE_teams])\n"
        "            AND TRUE\n"
        "        ORDER BY (SELECT NULL)\n"
        "        LIMIT :limit\n"
        "    "
    )


def test_calls_in_strings_and_comments_are_text() -> None:
    source = (
        "SELECT 'tpl.nope(', \"tpl.nope(\" -- tpl.nope(\n"
        "/* tpl.nope( */ x.tpl.nope(1), tpl.if_set(:a, 'it''s ) ,', 0)"
    )
    assert render(source, postgresql.dialect(), a=1) == (
        "SELECT 'tpl.nope(', \"tpl.nope(\" -- tpl.nope( "
        "/* tpl.nope( */ x.tpl.nope(1), 'it''s ) ,'"
    )


def test_a_call_may_hold_parentheses_and_commas() -> None:
    source = "tpl.if_set(:a, coalesce(x, (1, 2)), f(y, z))"
    assert render(source, postgresql.dialect(), a=None) == "f(y, z)"


def test_an_unknown_macro_is_refused_when_the_file_is_read() -> None:
    macros = sql_module.registered([for_teams, search, blue_or, bluish])
    available = ", ".join(sorted([*macros, "include"]))
    with pytest.raises(UnknownMacroError) as raised:
        render("SELECT 1\nWHERE tpl.foo(:x)", postgresql.dialect(), x=1)
    assert str(raised.value) == (
        f"Unknown macro tpl.foo in inline.sql:2; available: {available}. "
        "Register one with `Templates(..., macros=[...])`."
    )


def test_a_parameter_argument_must_be_a_parameter() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("WHERE tpl.if_set(name, TRUE)", postgresql.dialect())
    assert str(raised.value) == (
        "tpl.if_set: argument 1 must be a `:parameter`, got 'name' in inline.sql:1."
    )


def test_a_call_with_too_few_arguments_is_refused() -> None:
    with pytest.raises(MacroArgumentError, match="takes 2 to 3 arguments, got 1"):
        render("WHERE tpl.if_set(:a)", postgresql.dialect(), a=1)


def test_a_parameter_the_call_did_not_pass_is_refused() -> None:
    with pytest.raises(MacroArgumentError, match="`:q` was not passed"):
        render("WHERE x IN (tpl.each(:q))", postgresql.dialect())


def test_if_set_reads_a_parameter_the_call_did_not_pass_as_unset() -> None:
    source = "WHERE tpl.if_set(:q, tpl.icontains(name, :q)) AND x = :q"
    assert render(source, postgresql.dialect()) == "WHERE TRUE AND x = :q"


def test_a_parameter_nobody_passed_is_still_refused_outside_if_set() -> None:
    db = Database("sqlite://")
    with pytest.raises(StrayParameterError, match="`:q`"), db.connect():
        db.sql.from_string("SELECT tpl.if_set(:q, 1) WHERE 1 = :q").all()


def test_a_comma_inside_brackets_or_braces_stays_in_the_argument() -> None:
    source = "WHERE tpl.if_set(:x, ARRAY[1, 2] && tags, {'a': 1, 'b': 2})"
    dialect = postgresql.dialect()
    assert render(source, dialect, x=1) == "WHERE ARRAY[1, 2] && tags"
    assert render(source, dialect, x=None) == "WHERE {'a': 1, 'b': 2}"


def test_a_dollar_quoted_string_is_text() -> None:
    source = "SELECT $$ tpl.nope( $$, $fn$ ) tpl.nope( $fn$, a$b$c, tpl.if_set(:a, 1)"
    assert render(source, postgresql.dialect(), a=1) == (
        "SELECT $$ tpl.nope( $$, $fn$ ) tpl.nope( $fn$, a$b$c, 1"
    )


def test_icontains_takes_the_collation_snowflake_compares_under() -> None:
    from sqlalchemy.engine import default

    snowflake = default.DefaultDialect()
    snowflake.name = "snowflake"
    assert render("WHERE tpl.icontains(name, :q, 'en-ci-ai')", snowflake, q="é") == (
        "WHERE CONTAINS(COLLATE(name, 'en-ci-ai'), :q)"
    )


def test_a_coroutine_function_is_not_a_macro() -> None:
    async def later(value: Param) -> str:
        return str(value)

    with pytest.raises(MacroDefinitionError, match="coroutine function"):
        sql_macro(later)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "source",
    ["tpl.if_set(:a, (1)", "SELECT 'open", "SELECT /* open"],
)
def test_what_is_never_closed_is_refused(source: str) -> None:
    with pytest.raises(MacroSyntaxError, match=r"inline\.sql:1: .* never closed"):
        render(source, postgresql.dialect(), a=1)


def test_a_function_without_annotations_is_not_a_macro() -> None:
    def untyped(value) -> str:  # noqa: ANN001
        return value

    with pytest.raises(MacroDefinitionError, match="`value` is annotated `None`"):
        sql_macro(untyped)


def test_a_macro_cannot_take_a_builtin_name() -> None:
    @sql_macro(name="if_set")
    def mine(value: Param) -> str:
        return str(value)

    with pytest.raises(MacroDefinitionError, match="another macro has that name"):
        Templates(macros=[mine])


def test_check_reads_every_macro_template(tmp_path: Path) -> None:
    write(tmp_path, {"ok.sql": "SELECT 1", "bad/one.sql": "SELECT tpl.foo()"})
    db = Database("sqlite://", templates=tmp_path)
    with pytest.raises(UnknownMacroError, match=r"bad/one\.sql:1"):
        db.sql.check()


def test_check_passes_every_template(db: Database) -> None:
    db.sql.check()


def test_a_template_outside_the_paths_is_not_found(db: Database) -> None:
    with pytest.raises(FileNotFoundError), db.connect():
        db.sql("../etc/passwd.sql").all()


def test_auto_reload_reads_a_changed_file(tmp_path: Path) -> None:
    path = write(tmp_path, {"one.sql": "SELECT 1"}) / "one.sql"
    db = Database("sqlite://", templates=Templates(tmp_path, auto_reload=True))
    with db.connect():
        assert db.sql("one.sql").scalars().one() == 1
        path.write_text("SELECT 2")
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 1))
        assert db.sql("one.sql").scalars().one() == 2


def test_signature_says_how_a_template_calls_a_macro() -> None:
    assert [
        sql_module.signature_of(macro) for macro in sql_module.registered([]).values()
    ] == [
        "tpl.if_set(:value, expr[, otherwise])",
        "tpl.unless_set(:value, expr[, otherwise])",
        "tpl.if_not_set(:value, expr[, otherwise])",
        "tpl.order_by(:sort, column, *columns)",
        "tpl.icontains(column, text[, collation])",
        "tpl.icollate(column[, collation])",
        "tpl.json_object(*pairs)",
        "tpl.array_agg(value, *order_by)",
        "tpl.string_agg(value, separator, *order_by)",
        "tpl.array_contains(array, value)",
        "tpl.on_dialect(branch, *branches)",
        "tpl.between(column, :start, :end[, bounds])",
        "tpl.identifier(:name, *allowed)",
        "tpl.each(:values)",
        "tpl.in_list(column, :values, :exclude)",
        "tpl.array(:values[, type_name])",
        "tpl.arrays_overlap(array, other)",
        "tpl.array_contains_all(array, other)",
        "tpl.values(:rows)",
    ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"  # aiosqlite runs on asyncio


@pytest.mark.anyio
async def test_the_async_api_renders_the_same_macros(tmp_path: Path) -> None:
    from sqlakit.asyncio import Database as AsyncDatabase

    write(
        tmp_path,
        {
            "one.sql": "SELECT team FROM (SELECT 'red' AS team) WHERE tpl.blue_or(:teams)"
        },
    )
    db = AsyncDatabase(
        "sqlite+aiosqlite://", templates=Templates(tmp_path, macros=[blue_or])
    )
    async with db.connect():
        assert await db.sql("one.sql", teams=["red"]).scalars().all() == ["red"]
        assert await db.sql("one.sql", teams=["green"]).scalars().all() == []
    await db.dispose()


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("name", "name"),
        ("Mixed Name", '"Mixed Name"'),
        ('say "hi"', '"say ""hi"""'),
        (("reports", "Events"), 'reports."Events"'),
    ],
)
def test_identifier_quotes_a_name_only_where_it_has_to(value: Any, quoted: str) -> None:
    source = "SELECT tpl.identifier(:column)"
    assert render(source, postgresql.dialect(), column=value) == f"SELECT {quoted}"


def test_identifier_takes_only_the_names_it_lists() -> None:
    source = "SELECT tpl.identifier(:c, a.id, members = members_count)"
    dialect = postgresql.dialect()
    assert render(source, dialect, c="members") == "SELECT members_count"
    assert render(source, dialect, c="ID") == "SELECT a.id"
    with pytest.raises(UnknownIdentifierError, match="It takes: id, members"):
        render(source, dialect, c="password")


@pytest.mark.parametrize("value", [None, "", (), ("a", "")])
def test_identifier_refuses_an_empty_name(value: Any) -> None:
    with pytest.raises(UnknownIdentifierError):
        render("SELECT tpl.identifier(:c)", postgresql.dialect(), c=value)


def test_each_binds_each_value_as_a_parameter(db: Database) -> None:
    source = "WHERE team IN (tpl.each(:teams))"
    assert render(source, postgresql.dialect(), teams=["red", "blue"]) == (
        "WHERE team IN (:teams__1, :teams__2)"
    )
    assert names(db, "users/in_teams.sql", teams=["blue"]) == ["bob"]


def test_each_refuses_an_empty_list() -> None:
    with pytest.raises(MacroArgumentError, match="`:teams` is empty"):
        render("WHERE team IN (tpl.each(:teams))", postgresql.dialect(), teams=[])


def test_a_namespace_replaces_tpl_where_a_schema_has_that_name(tmp_path: Path) -> None:
    write(tmp_path, {"one.sql": "SELECT q.if_set(:x, 1, 2), tpl.f(1) FROM tpl.t"})
    db = Database(
        "sqlite://",
        templates=Templates(tmp_path, namespace="q", macros=[for_teams]),
    )
    statement = db.sql("one.sql", x=None).statement
    assert str(statement).splitlines()[-1] == "SELECT 2, tpl.f(1) FROM tpl.t"


def test_errors_name_the_namespace_in_use() -> None:
    template = "WHERE q.nope(:x)"
    with pytest.raises(UnknownMacroError, match=r"Unknown macro q\.nope in <string>"):
        sql_module.MacroTemplate(
            "<string>", template, sql_module.registered([]), 0, "q"
        )


def test_a_namespace_is_a_plain_name() -> None:
    with pytest.raises(ValueError, match="`namespace` is a plain name"):
        Templates(namespace="my schema")


def test_a_macros_own_refusal_says_where_the_call_is() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render(
            "SELECT 1\nWHERE x IN (tpl.each(:teams))",
            postgresql.dialect(),
            teams=[],
        )
    assert str(raised.value) == (
        "tpl.each: `:teams` is empty, and `IN ()` is not SQL in inline.sql:2."
    )


def test_a_macro_calls_a_builtin_one_as_a_template_would(db: Database) -> None:
    assert names(db, "users/search.sql", q="BLU") == ["bob"]
    assert names(db, "users/search.sql", q="") == ["Ann", "bob", "Cid", "dan_x"]
    assert render("WHERE tpl.search(:q, a, b)", postgresql.dialect(), q="x") == (
        "WHERE a ILIKE :q__like__1 ESCAPE '!' OR b ILIKE :q__like__2 ESCAPE '!'"
    )


def test_a_macro_binds_a_list_of_its_own_for_in(db: Database) -> None:
    assert names(db, "users/blue_or.sql", teams=["red"]) == [
        "Ann",
        "bob",
        "Cid",
        "dan_x",
    ]
    assert render("WHERE tpl.blue_or(:teams)", postgresql.dialect(), teams=["red"]) == (
        "WHERE team IN :__p1 OR team IN :teams"
    )


def test_a_value_where_a_parameter_goes_is_bound(db: Database) -> None:
    assert names(db, "users/bluish.sql") == ["bob"]
    assert render("WHERE tpl.bluish()", postgresql.dialect()) == (
        "WHERE team IN (SELECT column1 FROM (VALUES (:__p1)) AS v)"
    )


def test_a_macro_that_binds_cannot_be_called_outside_a_template() -> None:
    with pytest.raises(
        MacroArgumentError, match=r"tpl\.each: called outside a template"
    ):
        tpl.each([1])


def test_a_macro_that_only_writes_sql_can() -> None:
    assert tpl.if_set(Param("q", None), Sql("x = 1")) == "TRUE"


def test_a_plain_str_where_sql_goes_is_refused_from_python() -> None:
    ctx = Context("postgresql", postgresql.dialect().identifier_preparer, {})

    # A value from a request reaches a macro as a str, and would be written in.
    with pytest.raises(MacroArgumentError, match="argument 2 is SQL, and a plain"):
        tpl.icontains(ctx, Sql("name"), "x') OR 1=1 --")

    nested = tpl.if_set(
        ctx, Param("q", 1), tpl.icontains(ctx, Sql("name"), Param("q", "a"))
    )
    assert "ILIKE" in nested


def test_a_choice_is_checked_from_python() -> None:
    since, until = Param("since", 1), Param("until", 2)

    # `[)` without its quotes would leave the end in, as `'[]'` does.
    with pytest.raises(MacroArgumentError, match=r"argument 4 must be '\[\]' or"):
        tpl.between(Sql("at"), since, until, "[)")

    assert tpl.between(Sql("at"), since, until, "'[)'") == (
        "(at >= :since AND at < :until)"
    )


@pytest.mark.parametrize(
    ("values", "sql"),
    [
        ({}, "WHERE status <> 'archived'"),
        ({"status": None}, "WHERE status <> 'archived'"),
        ({"status": []}, "WHERE status <> 'archived'"),
        ({"status": "open"}, "WHERE TRUE"),
        ({"status": 0}, "WHERE TRUE"),
    ],
)
def test_unless_set_applies_only_when_the_value_is_not_there(
    values: dict[str, Any], sql: str
) -> None:
    source = "WHERE tpl.unless_set(:status, status <> 'archived')"
    assert render(source, postgresql.dialect(), **values) == sql


@pytest.mark.parametrize(
    "values", [{}, {"status": None}, {"status": ""}, {"status": "archived"}]
)
def test_if_not_set_writes_what_unless_set_writes(values: dict[str, Any]) -> None:
    unless = "WHERE tpl.unless_set(:status, status <> 'archived', FALSE)"
    written = render(unless, postgresql.dialect(), **values)

    if_not = unless.replace("unless_set", "if_not_set")

    assert render(if_not, postgresql.dialect(), **values) == written


def test_unless_set_takes_what_to_write_when_the_value_is_there() -> None:
    source = "WHERE tpl.unless_set(:ids, FALSE, id IN :ids)"
    assert render(source, postgresql.dialect(), ids=[1]) == "WHERE id IN :ids"
    assert render(source, postgresql.dialect(), ids=[]) == "WHERE FALSE"


def test_a_macro_names_the_values_it_binds() -> None:
    ctx = Context("postgresql", postgresql.dialect().identifier_preparer, {"x__1": 0})
    assert [ctx.bind(1, "x"), ctx.bind(2), ctx.bind(3, "x")] == [
        ":x__2",
        ":__p3",
        ":x__4",
    ]
    assert ctx.values == {"x__1": 0, "x__2": 1, "__p3": 2, "x__4": 3}


INCLUDES = {
    "members/ids.sql": """SELECT id FROM users
WHERE team IN :teams AND tpl.if_set(:q, tpl.icontains(name, :q));
""",
    "members/count.sql": (
        "SELECT count(*) FROM tpl.include('members/ids.sql') AS f\n"
        "WHERE tpl.icontains('x', :q) OR TRUE"
    ),
    "members/names.sql": """SELECT u.name
FROM users AS u
JOIN tpl.include('members/ids.sql') AS f ON f.id = u.id
ORDER BY u.id""",
    "loop/a.sql": "SELECT * FROM tpl.include('loop/b.sql') AS b",
    "loop/b.sql": "SELECT * FROM tpl.include('loop/a.sql') AS a",
    "broken/outer.sql": "SELECT 1\nFROM tpl.include('broken/inner.sql') AS i",
    "broken/inner.sql": "SELECT 1\nWHERE x IN (tpl.each(:missing))",
}


@pytest.fixture
def included(tmp_path: Path, db: Database) -> Database:
    db.templates = Templates(write(tmp_path, TEMPLATES | INCLUDES))
    return db


def test_include_puts_a_whole_query_in_place(included: Database) -> None:
    assert names(included, "members/names.sql", teams=["red"], q="n") == [
        "Ann",
        "dan_x",
    ]
    with included.connect():
        count = included.sql("members/count.sql", teams=["red", "blue"], q=None)
        assert count.scalars().one() == 4


def test_include_writes_the_query_in_parentheses_labelled_once(
    included: Database,
) -> None:
    with included.connect():
        statement = included.sql("members/count.sql", teams=["red"], q="a").statement
    assert str(statement) == (
        "/* members/count.sql */\n"
        "SELECT count(*) FROM (SELECT id FROM users\n"
        "WHERE team IN (__[POSTCOMPILE_teams]) AND lower(name) LIKE "
        "lower(:q__like__1) ESCAPE '!'\n) AS f\n"
        "WHERE lower('x') LIKE lower(:q__like__2) ESCAPE '!' OR TRUE"
    )


def test_include_refuses_a_template_that_includes_itself(included: Database) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        included.sql("loop/a.sql").statement
    assert str(raised.value) == (
        "tpl.include: includes itself: loop/a.sql -> loop/b.sql -> "
        "loop/a.sql in loop/b.sql:1 (included from loop/a.sql:1)."
    )


def test_check_finds_what_an_include_breaks(included: Database) -> None:
    with pytest.raises(MacroArgumentError, match="includes itself"):
        included.sql.check()


def test_an_error_in_an_included_template_says_where_it_was_included(
    included: Database,
) -> None:
    with pytest.raises(MacroArgumentError) as raised, included.connect():
        included.sql("broken/outer.sql").all()
    assert str(raised.value) == (
        "tpl.each: `:missing` was not passed in broken/inner.sql:2 "
        "(included from broken/outer.sql:2)."
    )


def test_a_missing_included_template_is_refused_on_load(tmp_path: Path) -> None:
    write(tmp_path, {"outer.sql": "SELECT 1\nFROM tpl.include('gone.sql') AS g"})
    db = Database("sqlite://", templates=tmp_path)
    with pytest.raises(
        MacroArgumentError,
        match=r"No SQL template named `gone\.sql`.* in outer\.sql:2\.",
    ):
        db.sql.check()


@pytest.mark.parametrize(
    ("argument", "written"),
    [
        (":name", "':name'"),
        ("'a.sql', 'b.sql'", "\"'a.sql', 'b.sql'\""),
    ],
)
def test_include_takes_one_path_written_out(argument: str, written: str) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        source = "SELECT * FROM tpl.include(" + argument + ") AS x"  # noqa: S608
        render(source, postgresql.dialect())
    assert str(raised.value) == (
        "tpl.include: takes the path of a template as a string, such as "
        f"'reports/ids.sql', got {written} in inline.sql:1."
    )


def test_an_included_query_drops_its_semicolon(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "one.sql": "SELECT 1 AS n;",
            "two.sql": "SELECT n FROM tpl.include('one.sql') AS o",
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    with db.connect():
        assert db.sql("two.sql").scalars().one() == 1


def test_include_is_not_a_name_a_macro_can_take() -> None:
    @sql_macro(name="include")
    def mine(value: Param) -> str:
        return str(value)

    with pytest.raises(MacroDefinitionError, match="another macro has that name"):
        Templates(macros=[mine])


def test_auto_reload_reads_a_changed_included_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "inner.sql": "SELECT 1 AS n",
            "outer.sql": "SELECT n FROM tpl.include('inner.sql') AS i",
        },
    )
    db = Database("sqlite://", templates=Templates(tmp_path, auto_reload=True))
    with db.connect():
        assert db.sql("outer.sql").scalars().one() == 1
        path = tmp_path / "inner.sql"
        path.write_text("SELECT 2 AS n")
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 1))
        assert db.sql("outer.sql").scalars().one() == 2


def test_an_included_template_is_read_once_while_its_file_stays(
    tmp_path: Path,
) -> None:
    write(tmp_path, {"inner.sql": "SELECT 1 AS n"})
    engine = Templates(tmp_path).engine
    outer = "SELECT n FROM tpl.include('inner.sql') AS i"

    first = engine.included("inner.sql", (("<string>", 1),))
    assert engine.included("inner.sql", (("<string>", 1),)) is first
    assert engine._from_string(outer).includes == {"inner.sql": first.mtime}

    path = tmp_path / "inner.sql"
    path.write_text("SELECT 2 AS n")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 1))
    assert engine.included("inner.sql", (("<string>", 1),)) is not first


def snowflake() -> sa.Dialect:
    dialect = default.DefaultDialect()
    dialect.name = "snowflake"
    return dialect


def mariadb() -> sa.Dialect:
    dialect = mysql.dialect()
    dialect.name = "mariadb"
    return dialect


# icollate


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (snowflake(), "ORDER BY COLLATE(name, 'und-ci-ai')"),
        (postgresql.dialect(), "ORDER BY lower(name)"),
        (sqlite.dialect(), "ORDER BY lower(name)"),
        (mysql.dialect(), "ORDER BY name"),
        (mariadb(), "ORDER BY name"),
    ],
)
def test_icollate_compares_without_case_on_each_dialect(
    dialect: sa.Dialect, sql: str
) -> None:
    assert render("ORDER BY tpl.icollate(name, 'und-ci-ai')", dialect) == sql


def test_icollate_is_en_ci_on_snowflake_unless_told() -> None:
    source = "WHERE tpl.icollate(email) = tpl.icollate(:email)"
    assert render(source, snowflake(), email="A") == (
        "WHERE COLLATE(email, 'en-ci') = COLLATE(:email, 'en-ci')"
    )


def test_icollate_sorts_under_order_by_without_case(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "sorted.sql": "SELECT name FROM (SELECT 'b' AS name UNION ALL "
            "SELECT 'a' UNION ALL SELECT 'C') AS t "
            "ORDER BY tpl.order_by(:sort, name = tpl.icollate(name))"
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    with db.connect():
        rows = db.sql("sorted.sql", sort="name").scalars().all()
        plain = db.sql.from_string(
            "SELECT name FROM (SELECT 'b' AS name UNION ALL SELECT 'a' "
            "UNION ALL SELECT 'C') AS t ORDER BY name"
        )
        assert plain.scalars().all() == ["C", "a", "b"]
    assert rows == ["a", "b", "C"]


# between


@pytest.mark.parametrize(
    ("start", "end", "closed", "half_open"),
    [
        (1, 9, "d BETWEEN :s AND :e", "(d >= :s AND d < :e)"),
        (1, None, "d >= :s", "d >= :s"),
        (None, 9, "d <= :e", "d < :e"),
        (None, None, "TRUE", "TRUE"),
        (0, "", "d >= :s", "d >= :s"),
    ],
)
def test_between_writes_what_the_ends_given_allow(
    start: Any, end: Any, closed: str, half_open: str
) -> None:
    dialect = postgresql.dialect()
    assert render("tpl.between(d, :s, :e)", dialect, s=start, e=end) == closed
    assert render("tpl.between(d, :s, :e, '[)')", dialect, s=start, e=end) == half_open


def test_between_reads_an_end_not_passed_as_missing() -> None:
    assert render("tpl.between(d, :s, :e)", postgresql.dialect(), s=1) == "d >= :s"


def test_between_refuses_other_bounds_when_the_file_is_read() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("WHERE tpl.between(d, :s, :e, '(]')", postgresql.dialect())
    assert str(raised.value) == (
        "tpl.between: argument 4 must be '[]' or '[)', got '(]' in inline.sql:1."
    )


# tpl.values


def test_values_writes_a_table_one_parameter_per_value() -> None:
    source = "SELECT * FROM tpl.values(:rows) AS v"
    rows = [(1, "a"), (2, "b")]
    assert render(source, postgresql.dialect(), rows=rows) == (
        "SELECT * FROM (VALUES (:rows__1, :rows__2), (:rows__3, :rows__4)) AS v"
    )
    assert render(source, mysql.dialect(), rows=rows) == (
        "SELECT * FROM (SELECT :rows__1 AS column1, :rows__2 AS column2 "
        "UNION ALL SELECT :rows__3, :rows__4) AS v"
    )
    assert render(source, mariadb(), rows=[1]) == (
        "SELECT * FROM (SELECT :rows__1 AS column1) AS v"
    )


def test_values_runs_as_a_table(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "segments.sql": "SELECT column1, column2 FROM tpl.values(:segments) "
            "AS v ORDER BY column1"
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    with db.connect():
        rows = db.sql("segments.sql", segments=[[2, "b"], (1, "a")]).all()
    assert [tuple(row) for row in rows] == [(1, "a"), (2, "b")]


@pytest.mark.parametrize(
    ("rows", "problem"),
    [
        ([], "`:rows` has no rows, and `VALUES` without one is not SQL"),
        ([(1, 2), (3,)], "row 2 of `:rows` has 1 values, and row 1 has 2"),
    ],
)
def test_values_refuses_what_is_not_a_table(rows: Any, problem: str) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("SELECT * FROM tpl.values(:rows) AS v", postgresql.dialect(), rows=rows)
    assert str(raised.value) == f"tpl.values: {problem} in inline.sql:1."


def test_a_literal_annotation_names_the_sql_an_argument_may_be() -> None:
    from typing import Literal

    @sql_macro
    def pick(which: Literal["'a'", "'b'"]) -> str:
        return which

    assert pick.slots[0].choices == ("'a'", "'b'")


def test_macros_are_imported_from_a_path() -> None:
    assert set(Templates(macros=[__name__]).macros) - set(
        sql_module.BUILTIN_MACROS
    ) == {
        "blue_or",
        "bluish",
        "for_teams",
        "search",
    }
    for path in (f"{__name__}:search", f"{__name__}.search"):
        assert Templates(macros=[path]).macros["search"] is search


def test_a_path_to_something_else_is_not_a_macro() -> None:
    with pytest.raises(MacroDefinitionError, match="it is not decorated @sql_macro"):
        Templates(macros=[f"{__name__}:write"])


def test_a_path_that_names_nothing_is_refused() -> None:
    with pytest.raises(UnknownImportPathError):
        Templates(macros=[f"{__name__}:nope"])


@pytest.mark.parametrize(
    ("inner", "included"),
    [
        ("SELECT 1 AS n -- the one", "(SELECT 1 AS n -- the one\n)"),
        ("SELECT 1 AS n; -- done\n", "(SELECT 1 AS n -- done\n)"),
        ("SELECT 1 AS n /* done */ ;\n\n", "(SELECT 1 AS n /* done */\n)"),
        ("SELECT ';' AS n", "(SELECT ';' AS n\n)"),
        ("SELECT $$;$$ AS n;", "(SELECT $$;$$ AS n\n)"),
    ],
)
def test_an_included_query_ends_where_its_sql_ends(
    tmp_path: Path, inner: str, included: str
) -> None:
    write(
        tmp_path,
        {
            "inner.sql": inner,
            "outer.sql": "SELECT n FROM tpl.include('inner.sql') AS i",
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    statement = str(db.sql("outer.sql").statement)
    expected = "SELECT n FROM " + included + " AS i"  # noqa: S608 - the test's own SQL
    assert statement.split("\n", 1)[1] == expected
    if "$$" not in inner:
        with db.connect():
            assert db.sql("outer.sql").scalars().one() in (1, ";")


def test_a_posix_class_is_not_a_parameter() -> None:
    db = Database("sqlite://")
    with db.connect():
        query = db.sql.from_string("SELECT '[[:punct:]][[:alpha:]]'")
        assert query.scalars().one() == "[[:punct:]][[:alpha:]]"


# parameters with a path


class Rotation(enum.Enum):
    IN = "in"
    OUT = "out"


@dataclass
class Criteria:
    teams: list[str]
    search: str | None = None

    @property
    def upper_teams(self) -> list[str]:
        return [team.upper() for team in self.teams]


def test_a_parameter_reads_an_attribute_or_a_key(db: Database, tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/criteria.sql": """
                SELECT name FROM users
                WHERE team IN :criteria.teams
                  AND tpl.if_set(:criteria.search, tpl.icontains(name, :criteria.search))
                  AND :filters.kind = 'people'
                ORDER BY id
            """
        },
    )
    criteria = Criteria(teams=["red"], search="n")
    assert names(
        db, "users/criteria.sql", criteria=criteria, filters={"kind": "people"}
    ) == [
        "Ann",
        "dan_x",
    ]


def test_a_path_reads_a_property_and_an_enum_value() -> None:
    source = "WHERE team IN :c.upper_teams AND way = :rotation.IN.value"
    ctx = Context(
        "postgresql",
        postgresql.dialect().identifier_preparer,
        {"c": Criteria(teams=["red"]), "rotation": Rotation},
    )
    template = sql_module.MacroTemplate("x.sql", source, sql_module.registered([]))
    assert template.render(ctx) == (
        "WHERE team IN :c__upper_teams AND way = :rotation__IN__value"
    )
    assert ctx.values["c__upper_teams"] == ["RED"]
    assert ctx.values["rotation__IN__value"] == "in"


def test_a_path_inside_a_string_or_a_comment_stays_text() -> None:
    source = "SELECT ':a.b', \"x:a.b\" -- :a.b\n/* :a.b */ FROM t WHERE 1::a.b"
    assert render(source, postgresql.dialect(), a={"b": 1}) == source.replace("\n", " ")


def test_a_path_whose_step_holds_a_double_underscore_is_refused() -> None:
    # `:a__b.c` and `:a.b__c` would both bind as `a__b__c`.
    with pytest.raises(MacroSyntaxError) as raised:
        render("SELECT 1\nWHERE x = :a__b.c", postgresql.dialect(), a__b={"c": 1})
    assert str(raised.value) == (
        "inline.sql:2: `:a__b.c` binds as `a__b__c`, which another path could too: "
        "a step of a path holds no `__`."
    )


def test_a_path_is_named_as_written_when_a_macro_refuses_it() -> None:
    with pytest.raises(MacroArgumentError, match=r"`:c\.rows` has no rows"):
        render(
            "SELECT * FROM tpl.values(:c.rows)", postgresql.dialect(), c={"rows": []}
        )
    with pytest.raises(MacroArgumentError, match=r"`:c\.teams` was not passed"):
        render("WHERE tpl.each(:c.teams)", postgresql.dialect())


def test_a_path_that_reads_nothing_is_refused() -> None:
    with pytest.raises(ParameterPathError) as raised:
        render("WHERE x = :c.nickname", postgresql.dialect(), c=Criteria(teams=[]))
    assert str(raised.value) == (
        "`:c.nickname` reads `nickname`, and the value before it has no key or attribute "
        "of that name."
    )


def test_if_set_reads_a_path_whose_root_was_not_passed_as_unset() -> None:
    assert (
        render("WHERE tpl.if_set(:c.search, x)", postgresql.dialect()) == "WHERE TRUE"
    )


def test_an_included_template_reads_paths_too(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "inner.sql": "SELECT :c.search AS s",
            "outer.sql": "SELECT s FROM tpl.include('inner.sql') AS i",
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    with db.connect():
        query = db.sql("outer.sql", c=Criteria(teams=[], search="x"))
        assert query.scalars().one() == "x"


def test_icontains_takes_an_expression_and_escapes_it_in_the_database() -> None:
    source = "WHERE tpl.icontains(city, trim(:name))"
    assert render(source, postgresql.dialect(), name="a") == (
        "WHERE city ILIKE '%' || replace(replace(replace(trim(:name), '!', '!!'), "
        "'%', '!%'), '_', '!_') || '%' ESCAPE '!'"
    )
    assert render(source, mysql.dialect(), name="a") == (
        "WHERE lower(city) LIKE lower(CONCAT('%', replace(replace(replace("
        "trim(:name), '!', '!!'), '%', '!%'), '_', '!_'), '%')) ESCAPE '!'"
    )


def test_icontains_matches_an_expression_only_as_itself(
    db: Database, tmp_path: Path
) -> None:
    write(
        tmp_path,
        {
            "users/like.sql": (
                "SELECT name FROM users WHERE tpl.icontains(name, lower(:q)) ORDER BY id"
            )
        },
    )
    assert names(db, "users/like.sql", q="_") == ["dan_x"]
    assert names(db, "users/like.sql", q="N") == ["Ann", "dan_x"]


# dialect macros


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (postgresql.dialect(), "JSON_BUILD_OBJECT('id', id)"),
        (snowflake(), "OBJECT_CONSTRUCT_KEEP_NULL('id', id)"),
        (mysql.dialect(), "JSON_OBJECT('id', id)"),
        (mariadb(), "JSON_OBJECT('id', id)"),
        (sqlite.dialect(), "json_object('id', id)"),
    ],
)
def test_json_object_builds_an_object_on_each_dialect(
    dialect: sa.Dialect, sql: str
) -> None:
    assert render("tpl.json_object('id', id)", dialect) == sql


@pytest.mark.parametrize(
    ("source", "postgres", "on_snowflake"),
    [
        ("tpl.array_agg(v)", "ARRAY_AGG(v)", "ARRAY_AGG(v)"),
        (
            "tpl.array_agg(v, a, b DESC)",
            "ARRAY_AGG(v ORDER BY a, b DESC)",
            "ARRAY_AGG(v) WITHIN GROUP (ORDER BY a, b DESC)",
        ),
        ("tpl.string_agg(v, ', ')", "STRING_AGG(v, ', ')", "LISTAGG(v, ', ')"),
        (
            "tpl.string_agg(v, ', ', a)",
            "STRING_AGG(v, ', ' ORDER BY a)",
            "LISTAGG(v, ', ') WITHIN GROUP (ORDER BY a)",
        ),
        (
            "tpl.array_contains(tags, :t)",
            ":t = ANY(tags)",
            "ARRAY_CONTAINS(:t::variant, tags)",
        ),
    ],
)
def test_aggregates_and_arrays_on_postgres_and_snowflake(
    source: str, postgres: str, on_snowflake: str
) -> None:
    assert render(source, postgresql.dialect(), t=1) == postgres
    assert render(source, snowflake(), t=1) == on_snowflake


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (mysql.dialect(), "GROUP_CONCAT(v ORDER BY a SEPARATOR ', ')"),
        (mariadb(), "GROUP_CONCAT(v ORDER BY a SEPARATOR ', ')"),
        (sqlite.dialect(), "group_concat(v, ', ' ORDER BY a)"),
    ],
)
def test_string_agg_on_the_other_databases(dialect: sa.Dialect, sql: str) -> None:
    assert render("tpl.string_agg(v, ', ', a)", dialect) == sql


@pytest.mark.parametrize("macro", ["array_agg(v)", "array_contains(a, 1)"])
def test_an_array_macro_refuses_a_database_without_arrays(macro: str) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render(f"SELECT tpl.{macro}", mysql.dialect())
    assert "has no form for mysql, only for postgresql, snowflake" in str(raised.value)


def test_json_object_and_string_agg_run(db: Database, tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/summary.sql": """
                SELECT team, tpl.string_agg(name, '/'), tpl.json_object('n', count(*))
                FROM (SELECT * FROM users ORDER BY id)
                GROUP BY team ORDER BY team
            """
        },
    )
    with db.connect():
        rows = [tuple(row) for row in db.sql("users/summary.sql").all()]
    assert rows == [("blue", "bob", '{"n":1}'), ("red", "Ann/Cid/dan_x", '{"n":3}')]


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (postgresql.dialect(), "FROM dim_country"),
        (snowflake(), "FROM facts.prod.dim_country"),
        (sqlite.dialect(), "FROM (SELECT 1)"),
    ],
)
def test_on_dialect_writes_the_branch_of_the_database(
    dialect: sa.Dialect, sql: str
) -> None:
    source = (
        "FROM tpl.on_dialect(postgresql = dim_country, "
        "snowflake = facts.prod.dim_country, default = (SELECT 1))"
    )
    assert render(source, dialect) == sql


def test_on_dialect_refuses_a_database_it_has_no_branch_for() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("FROM tpl.on_dialect(postgresql = a)\n", mysql.dialect())
    assert str(raised.value) == (
        "tpl.on_dialect: has no branch for mysql, and no `default = ...`: "
        "it has postgresql in inline.sql:1."
    )


def test_on_dialect_takes_only_named_branches() -> None:
    with pytest.raises(
        MacroArgumentError, match="takes `dialect = sql` branches, got 'a'"
    ):
        render("FROM tpl.on_dialect(a)", postgresql.dialect())


# optional values, clauses and arrays


def test_a_path_through_none_reads_as_none() -> None:
    ctx = Context(
        "postgresql",
        postgresql.dialect().identifier_preparer,
        {"filters": {"team": None}},
    )
    template = sql_module.MacroTemplate(
        "x.sql", "WHERE c = :filters.team.value", sql_module.registered([])
    )
    assert template.render(ctx) == "WHERE c = :filters__team__value"
    assert ctx.values["filters__team__value"] is None


def test_order_by_orders_by_nothing_when_the_sort_was_not_passed() -> None:
    assert render("ORDER BY tpl.order_by(:sort, id)", postgresql.dialect()) == (
        "ORDER BY (SELECT NULL)"
    )


def bound(source: str, dialect: sa.Dialect, **values: Any) -> str:
    """Return what a template becomes on a dialect, values written in where they decide."""
    template = sql_module.MacroTemplate("x.sql", source, sql_module.registered([]))
    sql, _ = sql_module._rendered(template, values, dialect.identifier_preparer)
    return sql


@pytest.mark.parametrize(
    ("dialect", "unlimited"),
    [
        (postgresql.dialect(), "ALL"),
        (snowflake(), "NULL"),
        (mysql.dialect(), "18446744073709551615"),
        (mariadb(), "18446744073709551615"),
        (sqlite.dialect(), "-1"),
    ],
)
def test_a_limit_without_a_value_takes_every_row(
    dialect: sa.Dialect, unlimited: str
) -> None:
    source = "LIMIT :limit OFFSET :offset"
    assert bound(source, dialect, limit=None, offset=None) == (
        f"LIMIT {unlimited} OFFSET 0"
    )
    assert bound(source, dialect, limit=0, offset=5) == source


def test_a_limit_without_a_value_runs(db: Database, tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/page.sql": (
                "SELECT name FROM users ORDER BY tpl.order_by(:sort, id) "
                "LIMIT :limit OFFSET :offset"
            )
        },
    )
    assert names(db, "users/page.sql", limit=None, offset=None) == [
        "Ann",
        "bob",
        "Cid",
        "dan_x",
    ]
    assert names(db, "users/page.sql", sort="id.desc", limit=2, offset=1) == [
        "Cid",
        "bob",
    ]


def test_a_list_in_brackets_binds_as_a_list(db: Database, tmp_path: Path) -> None:
    source = "WHERE team IN (:teams) AND id NOT IN ( :ids )"
    assert bound(source, postgresql.dialect(), teams=["a"], ids=(1, 2)) == (
        "WHERE team IN :teams AND id NOT IN :ids"
    )
    assert bound(source, postgresql.dialect(), teams="a", ids=1) == source
    write(
        tmp_path,
        {"users/in.sql": "SELECT name FROM users WHERE team IN (:teams) ORDER BY id"},
    )
    assert names(db, "users/in.sql", teams=["blue"]) == ["bob"]
    assert names(db, "users/in.sql", teams=[]) == []
    assert names(db, "users/in.sql", teams="blue") == ["bob"]


def test_the_same_sql_binds_as_each_call_asks() -> None:
    short = "WHERE team IN (:teams) LIMIT :limit"
    long = f"{short} -- {'x' * sql_module._CACHED_TEXT}"
    dialect = postgresql.dialect()

    for source in (short, long):
        rest = source.removeprefix(short)
        assert bound(source, dialect, teams=["a"], limit=None) == (
            f"WHERE team IN :teams LIMIT ALL{rest}"
        )
        assert bound(source, dialect, teams="a", limit=5) == source
        assert bound(source, dialect, teams=["a"], limit=None) == (
            f"WHERE team IN :teams LIMIT ALL{rest}"
        )


@pytest.mark.parametrize(
    ("source", "values", "postgres", "on_snowflake"),
    [
        (
            "tpl.array(:g, 'text')",
            ["a", "b"],
            "ARRAY[:g__1, :g__2]::text[]",
            "ARRAY_CONSTRUCT(:g__1, :g__2)",
        ),
        ("tpl.array(:g)", [1], "ARRAY[:g__1]", "ARRAY_CONSTRUCT(:g__1)"),
        ("tpl.array(:g, 'text')", [], "ARRAY[]::text[]", "ARRAY_CONSTRUCT()"),
        ("tpl.array(:g, 'varchar(20)')", None, "NULL::varchar(20)[]", "NULL"),
    ],
)
def test_array_writes_a_list_as_an_array(
    source: str, values: Any, postgres: str, on_snowflake: str
) -> None:
    assert render(source, postgresql.dialect(), g=values) == postgres
    assert render(source, snowflake(), g=values) == on_snowflake


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("tpl.array(:g)", "`:g` is empty, and PostgreSQL needs its type"),
        ("tpl.array(:g, 'text; DROP')", "takes a type name such as 'text'"),
    ],
)
def test_array_refuses_what_it_cannot_write(source: str, problem: str) -> None:
    with pytest.raises(MacroArgumentError, match=re.escape(problem)):
        render(source, postgresql.dialect(), g=[])


def test_only_the_branch_taken_is_rendered() -> None:
    ctx = Context("postgresql", postgresql.dialect().identifier_preparer, {"x": None})
    template = sql_module.MacroTemplate(
        "x.sql",
        "SELECT tpl.if_set(:x, tpl.each(:x), 0), tpl.if_set(:x, tpl.icontains(a, :x))",
        sql_module.registered([]),
    )
    assert template.render(ctx) == "SELECT 0, TRUE"
    assert ctx.values == {"x": None}


def test_order_by_falls_back_to_the_sort_the_template_names() -> None:
    source = "ORDER BY tpl.order_by(:sort, id, name, 'name.desc', 'id', 'nulls_last')"
    assert render(source, postgresql.dialect()) == (
        "ORDER BY name DESC NULLS LAST, id ASC NULLS LAST"
    )
    assert render(source, postgresql.dialect(), sort="id.desc") == (
        "ORDER BY id DESC NULLS LAST"
    )


@pytest.mark.parametrize(
    ("values", "exclude", "sql"),
    [
        (["a"], None, "WHERE s IN (:v)"),
        (["a"], True, "WHERE s NOT IN (:v)"),
        ([], True, "WHERE TRUE"),
        (None, None, "WHERE TRUE"),
    ],
)
def test_in_list_matches_a_list_or_everything_but_it(
    values: Any, exclude: Any, sql: str
) -> None:
    source = "WHERE tpl.in_list(s, :v, :x)"
    assert render(source, postgresql.dialect(), v=values, x=exclude) == sql


def test_in_list_runs(db: Database, tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "users/teams.sql": "SELECT name FROM users WHERE tpl.in_list(team, :t, :x) ORDER BY id"
        },
    )
    assert names(db, "users/teams.sql", t=["red"], x=True) == ["bob"]
    assert names(db, "users/teams.sql", t=["red"]) == ["Ann", "Cid", "dan_x"]


@pytest.mark.parametrize(
    ("source", "postgres", "on_snowflake"),
    [
        ("tpl.arrays_overlap(a, b)", "(a && b)", "ARRAYS_OVERLAP(a, b)"),
        (
            "tpl.array_contains_all(a, b)",
            "(a @> b)",
            "(ARRAY_SIZE(ARRAY_EXCEPT(b, a)) = 0)",
        ),
    ],
)
def test_array_comparisons_on_postgres_and_snowflake(
    source: str, postgres: str, on_snowflake: str
) -> None:
    assert render(source, postgresql.dialect()) == postgres
    assert render(source, snowflake()) == on_snowflake


# macros written in SQL

SQL_MACROS = """-- Macros of the application.

-- Rows of the tenant,
-- and of one team when asked.
SELECT t.tenant_id = :tenant_id AND tpl.if_set(:team_id, t.team_id = :team_id)
    AS for_tenant
FROM t;

SELECT 't' || t.name || :t = label AS labelled FROM t, label;

-- The tenant's rows, through another macro.
SELECT tpl.for_tenant(t) OR t.public AS scoped FROM t;
"""


@pytest.fixture
def macro_file(tmp_path: Path) -> Path:
    path = tmp_path / "_macros.sql"
    path.write_text(SQL_MACROS)
    return path


def test_sql_macros_are_read_from_their_headers(macro_file: Path) -> None:
    macros = sql_module.sql_macros(macro_file)
    assert [(one.name, one.params, one.doc, one.line) for one in macros] == [
        (
            "for_tenant",
            ("t",),
            "Rows of the tenant, and of one team when asked.",
            5,
        ),
        ("labelled", ("t", "label"), "", 9),
        ("scoped", ("t",), "The tenant's rows, through another macro.", 12),
    ]
    assert sql_module.signature_of(macros[1]) == "tpl.labelled(t, label)"


def test_an_sql_macro_writes_its_body_with_the_arguments(macro_file: Path) -> None:
    template = sql_module.MacroTemplate(
        "x.sql",
        "SELECT * FROM orders AS o WHERE tpl.for_tenant(o) AND tpl.labelled(o, 'x')",
        sql_module.registered([macro_file]),
    )
    ctx = Context(
        "postgresql",
        postgresql.dialect().identifier_preparer,
        {"tenant_id": 1, "team_id": None, "t": 2},
    )
    assert " ".join(template.render(ctx).split()) == (
        "SELECT * FROM orders AS o WHERE (o.tenant_id = :tenant_id AND TRUE) "
        "AND ('t' || o.name || :t = 'x')"
    )


def test_an_sql_macro_calls_another(macro_file: Path) -> None:
    template = sql_module.MacroTemplate(
        "x.sql", "WHERE tpl.scoped(o)", sql_module.registered([macro_file])
    )
    ctx = Context(
        "postgresql",
        postgresql.dialect().identifier_preparer,
        {"tenant_id": 1, "team_id": 2},
    )
    assert " ".join(template.render(ctx).split()) == (
        "WHERE ((o.tenant_id = :tenant_id AND o.team_id = :team_id) OR o.public)"
    )


def test_sql_macros_run(macro_file: Path, db: Database, tmp_path: Path) -> None:
    db.templates = Templates(tmp_path, macros=[str(macro_file)])
    write(
        tmp_path,
        {
            "users/red.sql": (
                "SELECT u.name FROM users AS u WHERE tpl.labelled(u, :label) "
                "ORDER BY u.id"
            )
        },
    )
    with db.connect():
        rows = db.sql("users/red.sql", t="", label="tAnn").scalars().all()
    assert rows == ["Ann"]


def test_an_sql_macro_that_expands_itself_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "loop.sql"
    path.write_text("SELECT tpl.b(x) AS a FROM x;\nSELECT tpl.a(x) AS b FROM x;\n")
    with pytest.raises(MacroArgumentError, match="expands itself: a -> b -> a"):
        sql_module.MacroTemplate(
            "x.sql", "SELECT tpl.a(1)", sql_module.registered([path])
        )


def test_an_error_in_an_sql_macro_says_where_it_is(tmp_path: Path) -> None:
    path = tmp_path / "broken.sql"
    path.write_text(
        "SELECT t.id = 1 AS ok FROM t;\n\nSELECT t.id = tpl.nope(1) AS bad FROM t;\n"
    )
    with pytest.raises(UnknownMacroError) as raised:
        sql_module.MacroTemplate(
            "x.sql", "SELECT\ntpl.bad(u)", sql_module.registered([path])
        )
    assert str(raised.value).startswith(
        "Unknown macro tpl.nope in broken.sql:3 (included from x.sql:2)"
    )


def test_an_sql_macro_takes_as_many_arguments_as_its_header_names(
    macro_file: Path,
) -> None:
    with pytest.raises(MacroArgumentError, match="takes 2 arguments, got 1"):
        sql_module.MacroTemplate(
            "x.sql", "SELECT tpl.labelled(o)", sql_module.registered([macro_file])
        )


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("SELECT TRUE;\n", "line 1 is not `SELECT <expression> AS <name>"),
        ("-- A note.\nt.id = 1;\n", "line 2 is not `SELECT <expression> AS <name>"),
        ("SELECT TRUE AS twice FROM a, a;\n", "name one twice"),
    ],
)
def test_a_statement_that_is_not_a_macro_is_refused(
    tmp_path: Path, source: str, problem: str
) -> None:
    path = tmp_path / "bad.sql"
    path.write_text(source)
    with pytest.raises(MacroDefinitionError, match=problem):
        sql_module.sql_macros(path)


def test_an_sql_macro_cannot_share_a_name(macro_file: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.sql"
    other.write_text("SELECT TRUE AS for_tenant FROM t;\n")
    with pytest.raises(MacroDefinitionError, match="another macro has that name"):
        Templates(macros=[macro_file, other])


def test_an_sql_macro_does_not_run_from_python(macro_file: Path) -> None:
    [macro, *_] = sql_module.sql_macros(macro_file)
    with pytest.raises(MacroArgumentError, match="is written in SQL"):
        macro("o")


def test_the_cli_lists_sql_macros(
    macro_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlakit._cli import main

    assert main(["macros", str(macro_file)]) == 0
    assert "tpl.for_tenant(t)\n    Rows of the tenant, and of one team when asked." in (
        capsys.readouterr().out
    )


def test_an_include_in_brackets_of_its_own_adds_none(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "inner.sql": "SELECT 1 AS n",
            "outer.sql": (
                "WITH one AS (tpl.include('inner.sql'))\n"
                "SELECT n FROM (tpl.include('inner.sql')) AS i"
            ),
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    assert str(db.sql("outer.sql").statement).split("\n", 1)[1] == (
        "WITH one AS (SELECT 1 AS n\n)\nSELECT n FROM (SELECT 1 AS n\n) AS i"
    )
    with db.connect():
        assert db.sql("outer.sql").scalars().one() == 1


@pytest.mark.parametrize(
    ("source", "values", "sql"),
    [
        (
            "WHERE tpl.if_set(:x, a IN (:x) OR b, FALSE) AND c",
            {"x": [1]},
            "WHERE (a IN (:x) OR b) AND c",
        ),
        ("WHERE NOT tpl.if_set(:x, a AND b)", {"x": 1}, "WHERE NOT (a AND b)"),
        ("WHERE tpl.unless_set(:x, a OR b) AND c", {}, "WHERE (a OR b) AND c"),
        ("WHERE tpl.if_set(:x, a IN (:x) OR b, FALSE)", {}, "WHERE FALSE"),
        ("WHERE tpl.if_set(:x, (a OR b))", {"x": 1}, "WHERE (a OR b)"),
        (
            "WHERE tpl.if_set(:x, a = 'this or that')",
            {"x": 1},
            "WHERE a = 'this or that'",
        ),
        ("WHERE tpl.if_set(:x, a_order = 1)", {"x": 1}, "WHERE a_order = 1"),
        ("ORDER BY tpl.if_set(:x, a DESC, b)", {"x": 1}, "ORDER BY a DESC"),
        ("FROM tpl.if_set(:x, orders, archive)", {}, "FROM archive"),
    ],
)
def test_a_branch_that_joins_conditions_stays_one(
    source: str, values: dict[str, Any], sql: str
) -> None:
    assert render(source, postgresql.dialect(), **values) == sql


def test_an_sql_macro_passes_its_argument_where_a_parameter_goes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "filters.sql"
    path.write_text(
        "SELECT tpl.if_set(negate, col NOT IN (vals), col IN (vals)) AS picked\n"
        "FROM col, vals, negate;\n"
        "\n"
        "SELECT tpl.arrays_overlap(col, tpl.array(vals, 'text')) AS overlaps\n"
        "FROM col, vals;\n"
    )
    source = "WHERE tpl.picked(u.country, :countries, :exclude) AND tpl.overlaps(u.tags, :d.v)"
    template = sql_module.MacroTemplate(
        source=source, name="x.sql", macros=sql_module.registered([path])
    )
    ctx = Context(
        "postgresql",
        postgresql.dialect().identifier_preparer,
        {"countries": ["de"], "exclude": True, "d": {"v": ["a"]}},
    )
    assert template.render(ctx) == (
        "WHERE (u.country NOT IN (:countries)) AND ((u.tags && ARRAY[:d__v__1]::text[]))"
    )


ARRAY_MATCH = """-- Rows whose array holds any of the values, or none of them when negated.
SELECT tpl.if_set(
    negate,
    NOT tpl.arrays_overlap(col, tpl.array(vals, 'text')),
    tpl.arrays_overlap(col, tpl.array(vals, 'text'))
) AS array_match
FROM col, vals, negate;
"""


def test_a_file_of_macros_among_the_templates_is_not_one(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "_macros.sql": ARRAY_MATCH,
            "tags.sql": "SELECT 1 WHERE tpl.array_match(f.tags, :f.v, :f.x)",
        },
    )
    templates = Templates(tmp_path, macros=[tmp_path / "_macros.sql"])
    assert templates.names() == ["tags.sql"]
    Database("sqlite://", templates=templates).sql.check()


def test_check_reads_the_body_of_a_macro_nothing_calls(tmp_path: Path) -> None:
    write(tmp_path, {"_macros.sql": "SELECT tpl.nope(x) AS unused FROM x;\n"})
    db = Database(
        "sqlite://", templates=Templates(tmp_path, macros=[tmp_path / "_macros.sql"])
    )
    with pytest.raises(UnknownMacroError, match=r"tpl\.nope in _macros\.sql:1"):
        db.sql.check()


# values written into the SQL


@pytest.mark.parametrize(
    ("name", "path", "written"),
    [
        ("exports", "", "@exports"),
        ("db.raw.exports", "orders/2026/", "@db.raw.exports/orders/2026/"),
        ("~", "a.csv", "@~/a.csv"),
        (
            "%orders",
            "date=2026-09-25/part-0.csv",
            "@%orders/date=2026-09-25/part-0.csv",
        ),
    ],
)
def test_a_stage_is_written_as_snowflake_names_one(
    name: str, path: str, written: str
) -> None:
    assert Inline.stage(name, path) == Inline(written)


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("a b", ""),
        ("x;drop", ""),
        ("exports", "../secrets"),
        ("exports", "a//b"),
        ("exports", "a b.csv"),
        ("exports", "a';--"),
        ("exports", "x--"),
        ("exports", "a/--b/"),
    ],
)
def test_a_stage_that_is_not_one_is_refused(name: str, path: str) -> None:
    with pytest.raises(InlineValueError):
        Inline.stage(name, path)


def test_a_name_is_plain_or_one_of_those_listed() -> None:
    assert Inline.name("orders_2026") == Inline("orders_2026")
    assert Inline.name("raw.orders") == Inline("raw.orders")
    assert Inline.name("b", "a", "b") == Inline("b")
    with pytest.raises(InlineValueError, match="is none of a, b"):
        Inline.name("c", "a", "b")
    with pytest.raises(InlineValueError, match="is not a plain name"):
        Inline.name("x; DROP TABLE users")
    with pytest.raises(TypeError, match="takes the text to write"):
        Inline(1)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "source",
    [
        "COPY INTO :v FROM (SELECT 1)",
        "COPY INTO orders FROM :v",
        "LIST :v",
        "PUT file:///tmp/a.csv :v",
        "CREATE TABLE :v (id INT)",
        "ALTER TABLE t RENAME TO :v",
        "USE SCHEMA :v",
        "SELECT * FROM :v",
        "SELECT * FROM t JOIN :v ON TRUE",
        "SELECT * FROM t SAMPLE (:v)",
        "SELECT * FROM t TABLESAMPLE BERNOULLI (:v)",
    ],
)
def test_an_inline_value_is_written_where_sql_takes_no_bound_one(source: str) -> None:
    assert bound(source, postgresql.dialect(), v=Inline("x_1")) == source.replace(
        ":v", "x_1"
    )


@pytest.mark.parametrize(
    ("source", "after"),
    [
        ("SELECT * FROM t WHERE x = :v", "x ="),
        ("SELECT * FROM t WHERE x IN (:v)", "IN ("),
        ("SELECT :v", "SELECT"),
        ("SELECT * FROM t LIMIT :v", "t LIMIT"),
    ],
)
def test_an_inline_value_where_a_bound_one_would_do_is_refused(
    source: str, after: str
) -> None:
    with pytest.raises(InlineValueError) as raised:
        bound(source, postgresql.dialect(), v=Inline("1"))
    assert str(raised.value).startswith(f"`v` stands after `{after}`, where SQL")


def test_an_inline_value_leaves_strings_and_comments_alone() -> None:
    source = "COPY INTO :v FROM (SELECT ':v' AS s -- :v\n)"
    assert bound(source, postgresql.dialect(), v=Inline("@s")) == (
        "COPY INTO @s FROM (SELECT ':v' AS s -- :v\n)"
    )


def test_an_inline_value_is_read_through_a_path() -> None:
    source = "COPY INTO :export.location FROM (SELECT 1)"
    assert bound(
        source, postgresql.dialect(), export={"location": Inline.stage("out", "a/")}
    ) == ("COPY INTO @out/a/ FROM (SELECT 1)")


def test_an_inline_value_runs_and_binds_nothing(tmp_path: Path) -> None:
    write(tmp_path, {"make.sql": "CREATE TABLE :name (id INT)"})
    db = Database(
        "sqlite://", engine_args={"poolclass": sa.StaticPool}, templates=tmp_path
    )
    with db.connect():
        statement = cast(
            "sa.TextClause", db.sql("make.sql", name=Inline.name("made")).statement
        )
        assert (str(statement).splitlines()[-1], statement.compile().params) == (
            "CREATE TABLE made (id INT)",
            {},
        )
        db.sql("make.sql", name=Inline.name("made")).execute()
        assert db.sql.from_string("SELECT count(*) FROM made").scalars().one() == 0


# macros whose SQL is in a file


@dataclass
class Tenant:
    id: int
    teams: list[str]


def tenant_macro(path: Path) -> Macro:
    @sql_macro(str(path))
    def for_tenant(t: Sql, tenant: Param) -> dict[str, Any]:
        """Rows of the tenant, and of its teams."""
        return {"tenant_id": tenant.value.id, "teams": tenant.value.teams}

    return for_tenant


TENANT_SQL = (
    "-- Rows of the tenant, and of its teams.\n"
    "SELECT t.tenant_id = :tenant_id AND tpl.if_set(:teams, t.team IN (:teams))"
    " AS for_tenant\n"
    "FROM t;\n"
)


def test_a_file_macro_binds_the_values_its_function_returns(tmp_path: Path) -> None:
    (tmp_path / "tenant.sql").write_text(TENANT_SQL)
    macros = sql_module.registered([tenant_macro(tmp_path / "tenant.sql")])
    template = sql_module.MacroTemplate(
        "x.sql",
        "SELECT * FROM a, b WHERE tpl.for_tenant(a, :one) AND tpl.for_tenant(b, :two)",
        macros,
    )
    sql, values = sql_module._rendered(
        template,
        {"one": Tenant(1, ["red"]), "two": Tenant(2, [])},
        postgresql.dialect().identifier_preparer,
    )
    first, second = re.findall(r"for_tenant_\d+", sql)[::2]
    assert first != second
    expected = (
        f"SELECT * FROM a, b WHERE (a.tenant_id = :{first}__tenant_id AND "  # noqa: S608
        f"a.team IN :{first}__teams) AND (b.tenant_id = :{second}__tenant_id AND TRUE)"
    )
    assert sql == expected
    assert {name: value for name, value in values.items() if "__" in name} == {
        f"{first}__tenant_id": 1,
        f"{first}__teams": ["red"],
        f"{second}__tenant_id": 2,
        f"{second}__teams": [],
    }


def test_a_file_macro_runs(tmp_path: Path) -> None:
    (tmp_path / "tenant.sql").write_text(TENANT_SQL)
    write(
        tmp_path / "sql",
        {
            "rows.sql": "SELECT n FROM (SELECT 1 AS n, 1 AS tenant_id, 'red' AS team) AS t0 WHERE tpl.for_tenant(t0, :tenant)"
        },
    )
    db = Database(
        "sqlite://",
        templates=Templates(
            tmp_path / "sql", macros=[tenant_macro(tmp_path / "tenant.sql")]
        ),
    )
    with db.connect():
        assert db.sql("rows.sql", tenant=Tenant(1, ["red"])).scalars().all() == [1]
        assert db.sql("rows.sql", tenant=Tenant(1, ["blue"])).scalars().all() == []
        assert db.sql("rows.sql", tenant=Tenant(2, [])).scalars().all() == []


@pytest.mark.parametrize(
    ("returned", "problem"),
    [
        ({"tenant_id": 1}, "returned no teams, which tenant.sql reads"),
        (
            {"tenant_id": 1, "teams": [], "extra": 2},
            "returned extra, which tenant.sql does not read",
        ),
        ([1], "returned list, not the values tenant.sql reads"),
    ],
)
def test_a_file_macro_returns_the_values_its_sql_reads(
    tmp_path: Path, returned: Any, problem: str
) -> None:
    (tmp_path / "tenant.sql").write_text(TENANT_SQL)

    @sql_macro(str(tmp_path / "tenant.sql"))
    def for_tenant(t: Sql) -> Any:
        return returned

    template = sql_module.MacroTemplate(
        "x.sql",
        "SELECT 1\nWHERE tpl.for_tenant(a)",
        sql_module.registered([for_tenant]),
    )
    with pytest.raises(MacroArgumentError) as raised:
        template.render(
            Context("postgresql", postgresql.dialect().identifier_preparer, {})
        )
    assert str(raised.value) == f"tpl.for_tenant: {problem} in x.sql:2."


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        (
            "SELECT TRUE AS other FROM t;\n",
            "tenant.sql has no `SELECT ... AS for_tenant`",
        ),
        (
            "SELECT u.x AS for_tenant FROM u;\n",
            "tenant.sql takes u after `FROM`, and the function takes no argument",
        ),
    ],
)
def test_a_file_macro_needs_its_statement(
    tmp_path: Path, sql: str, problem: str
) -> None:
    (tmp_path / "tenant.sql").write_text(sql)
    with pytest.raises(MacroDefinitionError, match=re.escape(problem)):
        tenant_macro(tmp_path / "tenant.sql")


def test_a_file_macro_in_a_file_of_macros_is_registered_once(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "tenant.macros.sql": TENANT_SQL + "\nSELECT TRUE AS other FROM t;\n",
            "a.sql": "SELECT 1",
        },
    )
    templates = Templates(
        tmp_path, macros=[tenant_macro(tmp_path / "tenant.macros.sql")]
    )
    assert isinstance(templates.macros["for_tenant"], sql_module.FileMacro)
    assert isinstance(templates.macros["other"], sql_module.SqlMacro)
    assert templates.names() == ["a.sql"]


def test_a_template_names_the_parameters_a_call_passes(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "inner.sql": "SELECT id FROM users WHERE team = :team",
            "outer.sql": (
                "SELECT * FROM tpl.include('inner.sql') AS i\n"
                "WHERE tpl.if_set(:q, tpl.icontains(name, :q))\n"
                "  AND kind = :filters.kind AND tpl.of_tenant(u, :tenant)\n"
                "  AND '::text' = :x::text\n"
                "LIMIT :limit"
            ),
        },
    )
    (tmp_path / "tenant.sql").write_text(
        "SELECT u.tenant_id = :tenant_id AS of_tenant FROM u;\n"
    )

    @sql_macro(str(tmp_path / "tenant.sql"))
    def of_tenant(u: Sql, tenant: Param) -> dict[str, Any]:
        return {"tenant_id": tenant.value}

    templates = Templates(tmp_path, macros=[of_tenant])
    template = templates.engine.get("outer.sql")

    assert template.parameters() == {"team", "q", "filters", "tenant", "x", "limit"}
