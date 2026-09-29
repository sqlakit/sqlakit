from __future__ import annotations

import bisect
import importlib
import importlib.util
import inspect
import itertools
import re
from collections.abc import Mapping, Sized
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, is_dataclass
from functools import cache, cached_property, lru_cache, partial
from inspect import iscoroutinefunction
from pathlib import Path
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    TypeAlias,
    TypeVar,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

import sqlalchemy as sa

from ._discovery import import_string
from ._query import _field_named, _parse_sort_field
from .exceptions import (
    Chain,
    InlineValueError,
    InvalidSortStringError,
    MacroArgumentError,
    MacroDefinitionError,
    MacroSyntaxError,
    MissingDependencyError,
    ParameterPathError,
    SQLNotConfiguredError,
    StrayParameterError,
    TemplateNotFoundError,
    UnknownIdentifierError,
    UnknownMacroError,
    UnknownOrderFieldError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from pydantic import BaseModel, TypeAdapter
    from sqlalchemy.sql import Executable

    from ._base import BaseDatabase
else:
    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:  # pragma: no cover - pydantic is installed in CI
        BaseModel = TypeAdapter = None

__all__ = [
    "BUILTIN_MACROS",
    "BaseSQLQuery",
    "Context",
    "FileMacro",
    "Inline",
    "Macro",
    "Param",
    "Sql",
    "SqlMacro",
    "Templates",
    "require_pydantic",
    "signature_of",
    "sql_macro",
    "sql_macros",
    "templates_of",
    "tpl",
]

_context: ContextVar[Context] = ContextVar("sqlakit.macro_context")

_calls_kept: ContextVar[bool] = ContextVar("sqlakit.calls_kept", default=False)


@contextmanager
def calls_kept() -> Iterator[None]:
    """Write a call that cannot be made with the values at hand as the call itself.

    An editor shows a template rendered with made-up values, or with none: a
    call that cannot take them stays a call, its arguments written out, and the
    rest renders. A macro read from its source, and never run, is such a call.
    Outside this block, the call raises as it always does.
    """
    token = _calls_kept.set(True)
    try:
        yield
    finally:
        _calls_kept.reset(token)


"""The call a macro template is rendering, for a macro that calls another."""

RowT = TypeVar("RowT")
DatabaseT = TypeVar("DatabaseT", bound="BaseDatabase[Any, Any]")
QueryT = TypeVar("QueryT", bound="BaseSQLQuery[Any, Any]")

PathLike = str | Path
"""A template directory."""


MACRO_FILE = "macros.sql"
"""The suffix that marks a file of SQL macros."""

NAMESPACE = "tpl"
"""The schema name macros are called under unless `Templates` says otherwise."""
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_PARAMETER = re.compile(r"\s*:([A-Za-z_]\w*(?:\.\w+)*)\s*")
_LITERAL = (
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*.*?\*/"""
    r"|\$(?P<tag>\w*)\$.*?\$(?P=tag)\$"
)
"""A string, a comment or a dollar-quoted body: text that may hold a colon."""

_DOTTED = re.compile(
    rf"{_LITERAL}|(?<![:\w\\]):(?P<param>[A-Za-z_]\w*(?:\.\w+)+)", re.DOTALL
)
"""A `:parameter.with.a.path`, past the strings and comments that may hold one."""
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")
_TYPE_NAME = re.compile(r"[A-Za-z_][\w ]*(?:\(\d+(?:, *\d+)?\))?")
_MACRO_STATEMENT = re.compile(
    r"\s*SELECT\s+(?P<body>.+)\s+AS\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s+FROM\s+(?P<args>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*))?\s*",
    re.IGNORECASE | re.DOTALL,
)
"""The statement an SQL macro is: `SELECT <expression> AS <name> FROM <arguments>`."""
_COLUMN_AS = re.compile(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.+?)\s*", re.DOTALL)
_BRANCH_NAMED = re.compile(r"\s*([A-Za-z_]\w*)\s*=(?!=)")
_NULLS_AFTER = re.compile(r"\s*NULLS\b", re.IGNORECASE)


class Param:
    """An argument written as `:name`: its name, and the value the call passed.

    `str(param)` is the placeholder, so a macro puts the parameter back into the
    SQL as it was written and it is bound like any other.
    """

    __slots__ = ("name", "value")

    def __init__(self, name: str, value: Any) -> None:  # noqa: ANN401
        self.name = name
        self.value = value

    def __str__(self) -> str:
        return f":{self.name}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, {self.value!r})"


class _PathParam(Param):
    """A `:a.b` argument, bound as `a__b`, that remembers how it was written."""

    __slots__ = ("path",)

    def __init__(self, name: str, value: Any, path: tuple[str, ...]) -> None:  # noqa: ANN401
        super().__init__(name, value)
        self.path = path


def _as_written(param: Param) -> str:
    """Return a parameter as the template wrote it: `:a.b`, not `:a__b`."""
    if isinstance(param, _PathParam):
        return ":" + ".".join(param.path)
    return str(param)


class _Value(Param):
    """A value passed where a `:parameter` goes, bound when the SQL names it.

    A macro that only reads the value binds nothing, and one that binds each
    item of a list binds only those.
    """

    __slots__ = ("_ctx", "_macro", "_placeholder")

    def __init__(self, value: Any, ctx: Context | None, macro: str) -> None:  # noqa: ANN401
        super().__init__("value", value)
        self._ctx = ctx
        self._macro = macro
        self._placeholder: str | None = None

    def __str__(self) -> str:
        if self._placeholder is None:
            if self._ctx is None:
                raise _outside_a_template(self._macro)
            self._placeholder = self._ctx.bind(self.value)
        return self._placeholder


def _named(param: Param) -> str | None:
    """Return the name to bind a value derived from a parameter under.

    A value passed in Python has no name of its own, and binds as `:__p1`.
    """
    return None if isinstance(param, _Value) else param.name


def _outside_a_template(macro: str) -> MacroArgumentError:
    problem = "called outside a template, where there is nothing to bind to"
    return MacroArgumentError(macro, problem)


class Inline:
    """A value written into the SQL, where SQL takes no bound value.

    ```python
    db.sql(
        "exports/orders.sql", location=Inline.stage("exports", f"orders/{day}/")
    )
    ```

    ```sql
    COPY INTO :location FROM (SELECT * FROM orders) FILE_FORMAT = (TYPE = CSV)
    ```

    A stage, a table or schema being created or renamed, a sample's size: SQL
    takes none of them as a parameter. The template still names it `:location`,
    so a linter reads it, and the value is written in as it is. Every other
    value is bound.

    Writing a value into SQL is how injection happens, so the value is written
    only after `INTO`, `FROM`, `JOIN`, `LIST`, `PUT <file>`, `TABLE`, `VIEW`,
    `STAGE`, `TO`, `SCHEMA`, `DATABASE`, `USE` and inside `SAMPLE (...)`, and
    refused where a bound value would do. `Inline.stage` and `Inline.name` check
    what they are given, and `Inline(text)` writes any text as it is.
    """

    __slots__ = ("text",)

    def __init__(self, text: str, /) -> None:
        if not isinstance(text, str):
            msg = f"`Inline` takes the text to write, a `str`, not {text!r}"
            raise TypeError(msg)
        self.text = text

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.text!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Inline) and other.text == self.text

    def __hash__(self) -> int:
        return hash((Inline, self.text))

    @classmethod
    def stage(cls, name: str, path: str = "") -> Inline:
        """Write a stage location: `@name`, or `@name/path`.

        ``name`` is a stage, qualified or not, `~` for the user's, or `%table`
        for a table's. ``path`` is parts of letters, digits, `_`, `.`, `-` and
        `=`, as a partition writes them, joined by `/` and ending in one for a
        prefix. `..`, `--` and an empty part are refused.

        Raises:
            InlineValueError: if either holds anything else.

        """
        if not _STAGE_NAME.fullmatch(name):
            raise InlineValueError(name, "is not the name of a stage")
        parts = path.split("/") if path else []
        prefix = bool(parts) and parts[-1] == ""
        for part in parts[:-1] if prefix else parts:
            # `--` would start a comment, and cut off what follows on the line.
            if part in (".", "..") or "--" in part or not _PATH_PART.fullmatch(part):
                raise InlineValueError(
                    path, f"holds `{part}`, which a stage path does not"
                )
        return cls("/".join([f"@{name}", *parts]))

    @classmethod
    def name(cls, value: str, *allowed: str) -> Inline:
        """Write a name: of a table, a schema, a view.

        With names after it, only one of those. Without, only a plain one:
        letters, digits and `_`, qualified with `.`, which needs no quoting.

        Raises:
            InlineValueError: if the value is neither.

        """
        if allowed and value not in allowed:
            raise InlineValueError(value, f"is none of {', '.join(allowed)}")
        if not _PLAIN_NAME.fullmatch(value):
            raise InlineValueError(value, "is not a plain name")
        return cls(value)


_STAGE_NAME = re.compile(r"~|%?[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*")
_PATH_PART = re.compile(r"[\w.=-]+")
_PLAIN_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


class Sql(str):
    """An argument as SQL text, with the macros inside it already expanded."""

    __slots__ = ()


class Context:
    """The call as a macro sees it, beyond its arguments.

    ``dialect`` is the name of the database's dialect, such as `postgresql` or
    `snowflake`. `bind` adds a value of the macro's own to the statement.
    """

    def __init__(
        self,
        dialect: str,
        preparer: Any,  # noqa: ANN401 - SQLAlchemy's IdentifierPreparer
        values: Mapping[str, Any],
    ) -> None:
        self.dialect = dialect
        self.preparer = preparer
        self.values: dict[str, Any] = dict(values)
        self._bound = 0

    @property
    def no_order(self) -> str:
        """An `ORDER BY` term that orders by nothing, for a macro with nothing to sort by.

        PostgreSQL and SQL Server refuse a bare `NULL` there, and take
        `(SELECT NULL)`. Snowflake refuses a subquery there, and takes `NULL`,
        as MySQL, SQLite and Oracle do. `0` would be a column's position.
        """
        return "(SELECT NULL)" if self.dialect in ("postgresql", "mssql") else "NULL"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.dialect!r})"

    def bind(self, value: Any, name: str | None = None) -> str:  # noqa: ANN401
        """Bind a value, and return the placeholder to write in its place.

        ``name`` names the value in the log and the debug server: `search_like`
        binds `:search_like__1`, and no name binds `:__p1`.
        """
        while True:
            self._bound += 1
            key = f"{name}__{self._bound}" if name else f"__p{self._bound}"
            if key not in self.values:
                break
        self.values[key] = value
        return f":{key}"

    def quote(self, name: str) -> str:
        """Return an identifier quoted the way this database quotes one."""
        return self.preparer.quote(name)


@dataclass(frozen=True, slots=True)
class _Slot:
    """One parameter of a macro, as a call fills it."""

    name: str
    kind: type[Param | Sql]
    default: Any = inspect.Parameter.empty
    choices: tuple[str, ...] = ()
    """The SQL a `Literal` annotation lets the argument be, written as in the template."""

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty


class Macro:
    """A function that `tpl.<name>(...)` calls, made by `sql_macro`.

    It stays callable as the function it wraps.
    """

    def __init__(
        self,
        func: Callable[..., str],
        name: str | None = None,
        *,
        optional: bool = False,
        lazy: bool = False,
    ) -> None:
        self.func = func
        self.name = (name or getattr(func, "__name__", "")).lower()
        self.doc = inspect.getdoc(func) or ""
        self.optional = optional
        self.lazy = lazy
        """Whether its SQL arguments render only when it reads them: a branch."""
        if iscoroutinefunction(func):
            raise MacroDefinitionError(
                self.name,
                "it is a coroutine function, and templates render synchronously: "
                "await the value and pass it in",
            )
        self.context, self.slots, self.variadic = _slots_of(func, self.name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    def __call__(self, *args: Any) -> str:  # noqa: ANN401
        """Call the macro from another one, the way a template calls it.

        ```python
        tpl.icontains(Sql("name"), q)
        tpl.each([1, 2, 3])
        ```

        The context is the render's, and a value where a `:parameter` goes is
        bound as one. Where SQL goes, a plain `str` is refused, as it could be a
        value from a request: write `Sql("...")` for SQL of your own. An
        argument the macro was given, a `Param`, and what another macro returns
        are SQL already.

        Raises:
            MacroArgumentError: if it needs a context and no template is rendering,
                or a plain `str` stands where SQL goes.

        """
        if args and isinstance(args[0], Context):
            ctx, args = args[0], args[1:]
        else:
            ctx = _context.get(None)
        if ctx is None and self.context:
            raise _outside_a_template(self.name)
        converted = [self._converted(index, arg, ctx) for index, arg in enumerate(args)]
        written = self.func(*([ctx] if self.context else []), *converted)
        # SQL for the next macro to take, as a macro writes SQL and not values.
        return Sql(written) if type(written) is str else written

    def _converted(self, index: int, arg: Any, ctx: Context | None) -> Any:  # noqa: ANN401
        kind = self._kind_or_none(index)
        if kind is Param and not isinstance(arg, Param):
            return _Value(arg, ctx, self.name)
        choices = self.slot_at(index).choices if kind is Sql else ()
        if choices:
            # One of a few pieces of SQL the macro names, so a plain `str` will do.
            if str(arg) not in choices:
                allowed = " or ".join(choices)
                problem = f"argument {index + 1} must be {allowed}, got {arg}"
                raise MacroArgumentError(self.name, problem)
            return Sql(arg)
        if kind is Sql and isinstance(arg, str) and not isinstance(arg, Sql):
            problem = (
                f"argument {index + 1} is SQL, and a plain `str` could be a value "
                f"from a request: write `Sql(...)` for SQL of your own, or pass "
                f"the `Param` for a value"
            )
            raise MacroArgumentError(self.name, problem)
        if kind is Sql and not isinstance(arg, Sql | _Deferred):
            return Sql(arg)
        return arg

    def _kind_or_none(self, index: int) -> type[Param | Sql] | None:
        """Return what an argument is, or None past what the macro takes."""
        if index < len(self.slots) or self.variadic is not None:
            return self.kind_at(index)
        return None

    @property
    def minimum(self) -> int:
        return sum(slot.required for slot in self.slots)

    @property
    def maximum(self) -> float:
        return float("inf") if self.variadic else len(self.slots)

    def kind_at(self, index: int) -> type[Param | Sql]:
        return self.slot_at(index).kind

    def slot_at(self, index: int) -> _Slot:
        if index < len(self.slots):
            return self.slots[index]
        assert self.variadic is not None  # noqa: S101 - the count was checked
        return self.variadic


class SqlMacro(Macro):
    """A macro written in SQL, in a file of them: see `sql_macros`.

    The body is the expression the `SELECT` writes, so the file is SQL a
    linter reads, its arguments declared as the tables it selects from. A call puts the body in its place, in brackets, when the
    template is read, with each argument's text where the body names it:
    `tpl.for_tenant(o)` writes `(o.tenant_id = ...)`. Parameters are the
    call's own, and the macros in the body expand as in the template.
    """

    def __init__(  # noqa: PLR0913 - what a header and a body say
        self,
        name: str,
        *,
        params: Sequence[str],
        body: str,
        doc: str,
        path: Path,
        line: int,
        body_line: int,
        name_at: tuple[int, int] | None = None,
    ) -> None:
        self.name = name.lower()
        self.doc = doc
        self.optional = False
        self.lazy = False
        self.context = False
        self.slots = tuple(_Slot(param, Sql) for param in params)
        self.variadic = None
        self.func = self._from_python
        self.params = tuple(params)
        self.body = body
        self.path = path
        self.line = line
        """The line of the header in the file."""
        self.body_line = body_line
        self.name_at = name_at or (line, 0)
        """The line and the column of its name in the file, as an editor goes to it."""
        names = "|".join(re.escape(param) for param in params) or r"(?!)"
        self._argument = re.compile(
            rf"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*.*?\*/"""
            rf"|(?<![\w.:\\])({names})(?!\w)",
            re.DOTALL,
        )

    @property
    def source_name(self) -> str:
        """The name errors in the body say, the file's."""
        return self.path.name

    def expanded(self, args: Sequence[str], body: str | None = None) -> str:
        """Return the body with the arguments where it names them, in brackets.

        The body is an expression, so the brackets keep it one wherever it
        goes: `a OR b` stays together after an `AND`. ``body`` stands in for
        the one the file holds, with its parameters named apart.
        """
        by_name = dict(zip(self.params, args, strict=True))
        written = self._argument.sub(
            lambda found: by_name[found.group(1)] if found.group(1) else found.group(),
            self.body if body is None else body,
        )
        return f"({written})"

    def __call__(self, *_: Any) -> Any:  # noqa: ANN401
        """Refuse, before the arguments: an SQL macro runs in a template."""
        return self._from_python()

    def _from_python(self, *_: Any) -> str:  # noqa: ANN401
        problem = "is written in SQL, and expands in a template, not in Python"
        raise MacroArgumentError(self.name, problem)


def sql_macros(path: Path | str, source: str | None = None) -> list[SqlMacro]:
    """Read the SQL macros of a file, each one a statement of its own.

    ```sql
    -- Rows of the tenant, and of one team when asked.
    SELECT t.tenant_id = :tenant_id AND tpl.if_set(:team_id, t.team_id = :team_id)
        AS for_tenant
    FROM t;
    ```

    The alias is the macro's name, the `FROM` lists its arguments, and a call
    writes the expression. The comment right above the statement is
    its description, and a blank line keeps a comment of the file's out of it.
    ``source`` is the file's text when it differs from what is saved.

    Raises:
        MacroDefinitionError: if a statement is not `SELECT <expression> AS
            <name> [FROM <arguments>];`, or names an argument twice.

    """
    path = Path(path)
    if source is None:
        source = path.read_text(encoding="utf-8")
    found = []
    for start, end in _statements(source, path.name):
        doc: list[str] = []
        offset = start
        for text_line in source[start:end].splitlines(keepends=True):
            stripped = text_line.strip()
            if stripped.startswith("--"):
                doc.append(stripped.removeprefix("--").strip())
            elif stripped:
                break
            else:
                doc = []
            offset += len(text_line)
        written = source[offset:end]
        if not written.strip():
            continue
        line = source.count("\n", 0, offset) + 1
        statement = _MACRO_STATEMENT.fullmatch(written)
        if statement is None:
            problem = (
                f"line {line} is not `SELECT <expression> AS <name> "
                f"[FROM <arguments>];`"
            )
            raise MacroDefinitionError(path.name, problem, line=line)
        name = statement.group("name")
        params = [
            one.strip()
            for one in (statement.group("args") or "").split(",")
            if one.strip()
        ]
        if len(set(params)) != len(params):
            problem = f"its arguments in {path.name}:{line} name one twice: {params}"
            raise MacroDefinitionError(name, problem, line=line)
        named = offset + statement.start("name")
        found.append(
            SqlMacro(
                name,
                params=params,
                body=statement.group("body").strip(),
                doc=" ".join(one for one in doc if one),
                path=path,
                line=line,
                body_line=line,
                name_at=(
                    source.count("\n", 0, named) + 1,
                    named - (source.rfind("\n", 0, named) + 1),
                ),
            )
        )
    return found


def _statements(source: str, name: str) -> list[tuple[int, int]]:
    """Return where each statement of a file starts and ends, at its `;`."""
    scanner = _Scanner(source, name, NAMESPACE)
    found = []
    start = index = 0
    while index < len(source):
        skipped = scanner.past_literal(index)
        if skipped != index:
            index = skipped
            continue
        if source[index] == ";":
            found.append((start, index))
            start = index + 1
        index += 1
    found.append((start, len(source)))
    return found


def sql_macro(
    func: Callable[..., str] | str | None = None,
    /,
    *,
    name: str | None = None,
    optional: bool = False,
    lazy: bool = False,
) -> Any:  # noqa: ANN401
    """Make a function a macro that templates call as `tpl.<name>(...)`.

    ```python
    @sql_macro
    def owned_by(team_ids: Param, user_ids: Param) -> str:
        return f"(team_id IN {team_ids} OR user_id IN {user_ids})"
    ```

    The annotations say what each argument is: a `Param` is written `:name` and
    brings its value, a `Sql` is the argument's text, and a `Context` first
    brings the dialect and a way to bind values of the macro's own.

    A `:name` the call did not pass is an error, unless ``optional`` is set: then
    it reads as `None`, for a macro whose point is that it may be missing.

    ``lazy`` renders a `Sql` argument only when the macro turns it into a
    string, for a macro that picks one branch of several: the others never run.

    Given a file, the SQL is in it, and the function returns the values it reads:

    ```python
    @sql_macro("filters.sql")
    def for_tenant(t: Sql, tenant: Param) -> dict[str, Any]:
        return {"tenant_id": tenant.value.id, "team_ids": tenant.value.team_ids}
    ```

    ```sql
    -- filters.sql, next to the module
    SELECT t.tenant_id = :tenant_id AND t.team_id IN (:team_ids) AS for_tenant
    FROM t;
    ```

    See `FileMacro`.

    Raises:
        MacroDefinitionError: if a parameter is annotated as none of them.

    """
    if isinstance(func, str):
        sql = func
        return lambda func: FileMacro(func, sql, name, optional=optional, lazy=lazy)
    if func is None:
        return lambda func: Macro(func, name, optional=optional, lazy=lazy)
    return Macro(func, name, optional=optional, lazy=lazy)


class FileMacro(Macro):
    """A macro whose SQL is in a file, and whose function gives the values.

    The file holds the SQL as a file of SQL macros does, `SELECT <expression>
    AS <name> FROM <arguments>;`, and the function's name picks the statement.
    Each argument after `FROM` is the function's argument of that name, written
    where the expression names it. Each `:name` in the expression is the
    macro's own: the function returns its value, which is bound, never written
    in. The parameters take names of their own in the statement, so they meet
    none of the calling template's.

    A path is from the module's directory, or absolute.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        sql: str | Path,
        name: str | None = None,
        *,
        optional: bool = False,
        lazy: bool = False,
    ) -> None:
        super().__init__(func, name, optional=optional, lazy=lazy)
        path = Path(sql)
        if not path.is_absolute():
            path = Path(inspect.getsourcefile(func) or ".").parent / path
        self.sql_path = path
        found = [one for one in sql_macros(path) if one.name == self.name]
        if not found:
            problem = f"{path.name} has no `SELECT ... AS {self.name}`"
            raise MacroDefinitionError(self.name, problem)
        self.statement = found[0]
        taken = {slot.name for slot in self.slots}
        if self.variadic is not None:
            taken.add(self.variadic.name)
        unknown = [one for one in self.statement.params if one not in taken]
        if unknown:
            problem = (
                f"{path.name} takes {', '.join(unknown)} after `FROM`, and the "
                f"function takes no argument of that name"
            )
            raise MacroDefinitionError(self.name, problem)
        dotted = next(
            (
                found.group("param")
                for found in _DOTTED.finditer(self.statement.body)
                if found.group("param")
            ),
            None,
        )
        if dotted is not None:
            problem = (
                f"{path.name} reads `:{dotted}`, and the function returns plain "
                f"values: read `:{dotted.replace('.', '_')}` and return it"
            )
            raise MacroDefinitionError(self.name, problem)
        self.reads = tuple(
            dict.fromkeys(
                found.group("param")
                for found in PARAMETER_IN_TEXT.finditer(self.statement.body)
                if found.group("param")
            )
        )
        """The parameters the SQL reads, whose values the function returns."""


_FILE_CALLS = itertools.count(1)
"""A number for each call of a `FileMacro`, which names its parameters apart."""


def _slots_of(
    func: Callable[..., Any], name: str
) -> tuple[bool, tuple[_Slot, ...], _Slot | None]:
    """Return whether a macro takes the context, its slots, and its `*args`."""
    hints = get_type_hints(func)
    context = False
    slots: list[_Slot] = []
    variadic = None
    for index, parameter in enumerate(inspect.signature(func).parameters.values()):
        kind = hints.get(parameter.name)
        if kind is Context and index == 0:
            context = True
            continue
        choices: tuple[str, ...] = ()
        if get_origin(kind) is Literal and all(
            isinstance(choice, str) for choice in get_args(kind)
        ):
            kind, choices = Sql, get_args(kind)
        if kind not in (Param, Sql):
            raise MacroDefinitionError(
                name,
                f"`{parameter.name}` is annotated `{kind}`: annotate it `Param`, "
                f"`Sql`, a `Literal` of the SQL it may be, or `Context` as the "
                f"first parameter",
            )
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            variadic = _Slot(parameter.name, kind, choices=choices)
        elif parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            if kind is Param and parameter.default is not inspect.Parameter.empty:
                raise MacroDefinitionError(
                    name, f"`{parameter.name}` is a `Param` with a default"
                )
            slots.append(_Slot(parameter.name, kind, parameter.default, choices))
        else:
            raise MacroDefinitionError(
                name,
                f"`{parameter.name}` is keyword-only, and a template passes "
                f"arguments by position",
            )
    return context, tuple(slots), variadic


def signature_of(macro: Macro, namespace: str = NAMESPACE) -> str:
    """Return how a template calls a macro: `tpl.if_set(:p, expr[, otherwise])`."""
    written = ""
    for index, slot in enumerate(macro.slots):
        shown = f":{slot.name}" if slot.kind is Param else slot.name
        separator = ", " if index else ""
        written += f"{separator}{shown}" if slot.required else f"[{separator}{shown}]"
    if macro.variadic is not None:
        separator = ", " if macro.slots else ""
        shown = macro.variadic.name
        written += f"{separator}*{':' if macro.variadic.kind is Param else ''}{shown}"
    return f"{namespace}.{macro.name}({written})"


@dataclass(frozen=True, slots=True)
class _Arg:
    parts: tuple[str | _Call, ...]
    param: str | None
    """The name, when the argument is `:name` and nothing else."""
    span: tuple[int, int]
    """The argument's place in the template, without the space around it."""


@dataclass(frozen=True, slots=True)
class _Call:
    name: str
    args: tuple[_Arg, ...]
    line: int
    span: tuple[int, int]
    """The call's place in the template, from the namespace to its `)`."""


@cache
def _patterns(namespace: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Return where the scanner has something to decide, and what a call is."""
    prefix = re.escape(namespace)
    decide = re.compile(
        rf"""['"$(\[{{)\]}},]|--|/\*|(?<![\w.]){prefix}\.""", re.IGNORECASE
    )
    call = re.compile(rf"{prefix}\.([A-Za-z_]\w*)\s*\(", re.IGNORECASE)
    return decide, call


class _Scanner:
    """Cut a template into text and `tpl.` calls, past strings and comments."""

    def __init__(
        self,
        source: str,
        template: str,
        namespace: str,
        chain: Chain = (),
        first_line: int = 1,
    ) -> None:
        self.source = source
        self.template = template
        self.namespace = namespace
        self.chain = chain
        self.first_line = first_line
        """The line of its file the text starts on: an SQL macro's body is not the top."""
        self.decide, self.call = _patterns(namespace)

    def parts(self) -> tuple[str | _Call, ...]:
        return self._scan(0, in_args=False)[0]

    def _scan(
        self, start: int, *, in_args: bool
    ) -> tuple[tuple[str | _Call, ...], int, str | None]:
        """Read up to the end, or to the `,` or `)` that ends an argument."""
        source = self.source
        parts: list[str | _Call] = []
        text_from = index = start
        depth = 0
        while found := self.decide.search(source, index):
            index = found.start()
            char = source[index]
            if (skipped := self.past_literal(index)) != index:
                index = skipped
            elif (call := self.call.match(source, index)) and not self._inside_name(
                index
            ):
                parts.append(source[text_from:index])
                args, index = self._args(call.end())
                parts.append(
                    _Call(
                        call.group(1).lower(),
                        args,
                        self._line(call.start()),
                        (call.start(), index),
                    )
                )
                text_from = index
            elif in_args and char in "([{":
                depth += 1
                index += 1
            elif in_args and char in ")]}" and depth:
                depth -= 1
                index += 1
            elif in_args and (char == ")" or (char == "," and not depth)):
                parts.append(source[text_from:index])
                return _joined(parts), index, char
            else:
                index += 1
        parts.append(source[text_from:])
        return _joined(parts), len(source), None

    def _args(self, start: int) -> tuple[tuple[_Arg, ...], int]:
        """Read a call's arguments, and return where the call ends."""
        args = []
        index = start
        while True:
            begin = index
            parts, index, closer = self._scan(index, in_args=True)
            if closer is None:
                problem = f"a `{self.namespace}.` call is never closed"
                raise self._error(problem, start - 1)
            args.append(_argument(parts, self._trimmed(begin, index)))
            index += 1
            if closer == ")":
                break
        if len(args) == 1 and args[0].parts == ():
            args = []
        return tuple(args), index

    def past_literal(self, index: int) -> int:
        """Return where a string, a quoted name or a comment starting here ends.

        When none starts there, the offset comes back unchanged.
        """
        source = self.source
        if source[index] in "'\"":
            return self._past_quoted(index, source[index])
        dollar = _DOLLAR_QUOTE.match(source, index)
        if dollar and not self._inside_name(index):
            return self._past(dollar.group(), dollar.end(), "a dollar-quoted string")
        if source.startswith("--", index):
            newline = source.find("\n", index)
            return len(source) if newline < 0 else newline
        if source.startswith("/*", index):
            return self._past("*/", index + 2, "a comment")
        return index

    def _past(self, closer: str, start: int, what: str) -> int:
        """Return where ``closer`` ends, or say that ``what`` is never closed."""
        end = self.source.find(closer, start)
        if end < 0:
            problem = f"{what} is never closed"
            raise self._error(problem, start)
        return end + len(closer)

    def _past_quoted(self, start: int, quote: str) -> int:
        """Return where a string or a quoted name ends; a doubled quote is inside it."""
        index = start + 1
        while True:
            index = self.source.find(quote, index)
            if index < 0:
                problem = "a quoted string is never closed"
                raise self._error(problem, start)
            if self.source.startswith(quote * 2, index):
                index += 2
                continue
            return index + 1

    def _inside_name(self, index: int) -> bool:
        """Whether `tpl.` here is the tail of a longer name, as in `x.tpl.f(`."""
        before = self.source[index - 1] if index else ""
        return before == "." or before.isalnum() or before == "_"

    def _trimmed(self, start: int, end: int) -> tuple[int, int]:
        """Return a span without the space at either end."""
        text = self.source[start:end]
        lead = len(text) - len(text.lstrip())
        return start + lead, max(start + lead, end - (len(text) - len(text.rstrip())))

    def _line(self, index: int) -> int:
        return bisect.bisect_right(self._newlines, index - 1) + self.first_line

    @cached_property
    def _newlines(self) -> list[int]:
        """The offset of each line break, for finding a line without counting."""
        return [found.start() for found in re.finditer("\n", self.source)]

    def _error(self, problem: str, index: int) -> MacroSyntaxError:
        return MacroSyntaxError(
            self.template,
            self._line(index),
            problem,
            chain=self.chain,
            span=(index, index + 1),
        )


def _joined(parts: list[str | _Call]) -> tuple[str | _Call, ...]:
    return tuple(part for part in parts if part != "")


def _argument(parts: tuple[str | _Call, ...], span: tuple[int, int]) -> _Arg:
    """Return an argument, trimmed, and whether it is a parameter alone."""
    trimmed = list(parts)
    if trimmed and isinstance(trimmed[0], str):
        trimmed[0] = trimmed[0].lstrip()
    if trimmed and isinstance(trimmed[-1], str):
        trimmed[-1] = trimmed[-1].rstrip()
    trimmed_parts = _joined(trimmed)
    param = None
    if len(trimmed_parts) == 1 and isinstance(trimmed_parts[0], str):
        match = _PARAMETER.fullmatch(trimmed_parts[0])
        param = match.group(1) if match else None
    return _Arg(trimmed_parts, param, span)


INCLUDE = "include"
"""The call that puts a whole query from another template in place, as `(...)`."""

_QUOTED_PATH = re.compile(r"'([^']+)'")


class MacroTemplate:
    """A `.sql` template, read and checked against the macros it calls.

    `tpl.include('other.sql')` puts the query of another template in its
    place, in parentheses, where a table goes:

    ```sql
    SELECT u.id FROM tpl.include('users/search.sql') AS u
    ```

    The path is a string, read with the file: a template that is missing, or
    that includes itself through others, is refused there. The included query
    shares the parameters of the call, and loses a trailing `;`.
    """

    def __init__(  # noqa: PLR0913 - what a template is read with
        self,
        name: str,
        source: str,
        macros: Mapping[str, Macro],
        mtime: float = 0,
        namespace: str = NAMESPACE,
        *,
        include: Callable[[str, Chain], MacroTemplate] | None = None,
        chain: tuple[tuple[str, int], ...] = (),
        expanding: tuple[str, ...] = (),
        first_line: int = 1,
    ) -> None:
        self.name = name
        self.source = source
        self.expanding = expanding
        """The SQL macros being expanded into this text, outermost first."""
        self.mtime = mtime
        self.macros = macros
        self.namespace = namespace
        self.include = include
        """Return an included template, read in the chain of includes given."""
        self.chain = chain
        """The templates this one was included from, and the line of each call."""
        self.includes: dict[str, float] = {}
        """Every template this one includes, however deep, and when it changed."""
        self.paths: dict[str, tuple[str, ...]] = {}
        """Every `:a.b` read here, by the name it binds as, `a__b`, and its path."""
        self.parts = _Scanner(source, name, namespace, chain, first_line).parts()
        self._check_paths(first_line)
        self._check(self.parts)
        self._compiled = self._compile(self.parts)

    def _check_paths(self, first_line: int) -> None:
        """Refuse a `:a.b` that binds under the name another one would."""
        for found in _DOTTED.finditer(self.source):
            path = found.group("param")
            if path is not None and "__" in path:
                line = first_line + self.source.count("\n", 0, found.start())
                problem = (
                    f"`:{path}` binds as `{path.replace('.', '__')}`, which "
                    f"another path could too: a step of a path holds no `__`"
                )
                span = (found.start(), found.end())
                raise MacroSyntaxError(
                    self.name, line, problem, chain=self.chain, span=span
                )

    def parameters(self) -> frozenset[str]:
        """Return the names a call passes values under.

        That is every `:name` the template reads: in its text, in the templates
        it includes, and as an argument of a macro. `:a.b` is `a`. A parameter a
        macro binds itself, as `@sql_macro("file.sql")` does, is not one.
        """
        roots = {key: path[0] for key, path in self.paths.items()}
        found: set[str] = set()

        def read(parts: Sequence[_Part], internal: str | None = None) -> None:
            for part in parts:
                if isinstance(part, str):
                    for match in PARAMETER_IN_TEXT.finditer(part):
                        name = match.group("param")
                        if name and not (internal and name.startswith(internal)):
                            found.add(roots.get(name, name))
                elif isinstance(part, _FileExpansion):
                    read([part.call])
                    read(part.parts, f"{part.prefix}__")
                else:
                    found.update(roots.get(name, name) for name in part.params)
                    for _, argument in part.args:
                        read(argument or ())

        read(self._compiled)
        return frozenset(found)

    def render(self, ctx: Context) -> str:
        """Return the SQL for this call, the values it bound going to ``ctx``."""
        for key, (root, *path) in self.paths.items():
            if root in ctx.values and key not in ctx.values:
                ctx.values[key] = _followed(ctx.values[root], root, path)
        token = _context.set(ctx)
        try:
            return self._render(self._compiled, ctx)
        finally:
            _context.reset(token)

    def _compile(self, parts: Sequence[str | _Call]) -> tuple[_Part, ...]:
        """Resolve every call to its macro once, so rendering only calls them.

        An included template is read here, and its pieces become these.
        """
        compiled: list[_Part] = []
        for index, part in enumerate(parts):
            if isinstance(part, str):
                compiled.append(_DOTTED.sub(self._flattened, part))
            elif part.name == INCLUDE:
                # `)` on a line of its own: the query may end in a `--` comment.
                if _bracketed(parts, index):
                    compiled.extend((*self._include(part), "\n"))
                else:
                    compiled.extend(("(", *self._include(part), "\n)"))
            elif isinstance(macro := self.macros[part.name], SqlMacro):
                compiled.extend(self._inline(part, macro))
            elif isinstance(macro, FileMacro):
                compiled.append(self._from_file(part, macro))
            else:
                compiled.append(self._expansion(part))
        return tuple(compiled)

    def _from_file(self, call: _Call, macro: FileMacro) -> _FileExpansion:
        """Read a `FileMacro`'s SQL into this call, its parameters named apart."""
        if macro.name in self.expanding:
            cycle = " -> ".join((*self.expanding, macro.name))
            raise self._refuse(macro.name, f"expands itself: {cycle}", call)
        prefix = f"{macro.name}_{next(_FILE_CALLS)}"
        body = PARAMETER_IN_TEXT.sub(
            lambda found: (
                f":{prefix}__{found.group('param')}"
                if found.group("param")
                else found.group()
            ),
            macro.statement.body,
        )
        slots = [slot.name for slot in macro.slots]
        # An argument the call leaves out writes its default.
        written = {
            slot.name: str(slot.default) for slot in macro.slots if not slot.required
        }
        written.update(
            (name, self.source[slice(*arg.span)])
            for name, arg in zip(slots, call.args, strict=False)
        )
        text = macro.statement.expanded(
            [written.get(name, name) for name in macro.statement.params], body
        )
        inlined = MacroTemplate(
            macro.statement.source_name,
            text,
            self.macros,
            namespace=self.namespace,
            include=self.include,
            chain=(*self.chain, (self.name, call.line)),
            expanding=(*self.expanding, macro.name),
            first_line=macro.statement.body_line,
        )
        self.includes.update(inlined.includes)
        self.paths.update(inlined.paths)
        return _FileExpansion(
            self._expansion(call),
            prefix,
            macro.reads,
            macro.sql_path.name,
            inlined._compiled,
        )

    def _inline(self, call: _Call, macro: SqlMacro) -> tuple[_Part, ...]:
        """Write an SQL macro's body in place of the call, read like this text."""
        if macro.name in self.expanding:
            cycle = " -> ".join((*self.expanding, macro.name))
            raise self._refuse(macro.name, f"expands itself: {cycle}", call)
        written = [self.source[slice(*arg.span)] for arg in call.args]
        inlined = MacroTemplate(
            macro.source_name,
            macro.expanded(written),
            self.macros,
            namespace=self.namespace,
            include=self.include,
            chain=(*self.chain, (self.name, call.line)),
            expanding=(*self.expanding, macro.name),
            first_line=macro.body_line,
        )
        self.includes.update(inlined.includes)
        self.paths.update(inlined.paths)
        return inlined._compiled

    def _include(self, call: _Call) -> tuple[_Part, ...]:
        path = _QUOTED_PATH.fullmatch(self._written(call.args[0].parts))
        assert path is not None  # noqa: S101 - checked on load
        name = path.group(1)
        chain = (*self.chain, (self.name, call.line))
        if name in {template for template, _ in chain}:
            cycle = " -> ".join((*(template for template, _ in chain), name))
            raise self._refuse(INCLUDE, f"includes itself: {cycle}", call)
        if self.include is None:
            problem = "has no template paths to read from"
            raise self._refuse(INCLUDE, problem, call)
        try:
            included = self.include(name, chain)
        except TemplateNotFoundError as error:
            raise self._refuse(INCLUDE, str(error).rstrip("."), call) from None
        self.includes.update({name: included.mtime, **included.includes})
        self.paths.update(included.paths)
        return included._compiled  # noqa: SLF001 - a template of this class

    def _flattened(self, match: re.Match[str]) -> str:
        """Write `:a.b` as the parameter it binds as, and leave the rest alone."""
        path = match.group("param")
        return match.group() if path is None else f":{self._key(path)}"

    def _key(self, param: str) -> str:
        """Return the name a parameter binds as: `a.b` as `a__b`, `a` as itself."""
        if "." not in param:
            return param
        path = tuple(param.split("."))
        key = "__".join(path)
        self.paths[key] = path
        return key

    def _expansion(self, call: _Call) -> _Expansion:
        macro = self.macros[call.name]
        args = tuple(
            (self._key(arg.param), None)
            if macro.kind_at(index) is Param and arg.param is not None
            else (None, self._compile(arg.parts))
            for index, arg in enumerate(call.args)
        )
        params = tuple(param for param, _ in args if param is not None)
        return _Expansion(macro, call.line, args, params, self.name, self.chain)

    def _check(self, parts: Sequence[str | _Call]) -> None:
        """Refuse an unknown macro or a call it cannot take, where the file is read.

        Raises:
            UnknownMacroError: naming the macro and what there is.
            MacroArgumentError: if a call has the wrong arguments.

        """
        for index, part in enumerate(parts):
            if isinstance(part, str):
                continue
            after = parts[index + 1] if index + 1 < len(parts) else ""
            if (
                part.name == "order_by"
                and isinstance(after, str)
                and _NULLS_AFTER.match(after)
            ):
                problem = (
                    "`NULLS` after the call repeats the one a sort string writes: "
                    "pass 'nulls_last' or 'nulls_first' as an argument"
                )
                raise self._refuse(part.name, problem, part)
            if part.name == INCLUDE:
                self._check_include(part)
                continue
            macro = self.macros.get(part.name)
            if macro is None:
                raise UnknownMacroError(
                    part.name,
                    self.name,
                    part.line,
                    [*self.macros, INCLUDE],
                    namespace=self.namespace,
                    chain=self.chain,
                    span=part.span,
                )
            count = len(part.args)
            if not macro.minimum <= count <= macro.maximum:
                problem = f"takes {_arity(macro)} arguments, got {count}"
                raise self._refuse(macro.name, problem, part)
            for position, arg in enumerate(part.args):
                slot = macro.slot_at(position)
                written = self._written(arg.parts)
                if slot.kind is Param and arg.param is None:
                    problem = f"argument {position + 1} must be a `:parameter`, got {written!r}"
                    raise self._refuse(macro.name, problem, part, arg.span)
                if slot.choices and written not in slot.choices:
                    allowed = " or ".join(slot.choices)
                    problem = (
                        f"argument {position + 1} must be {allowed}, got {written}"
                    )
                    raise self._refuse(macro.name, problem, part, arg.span)
                self._check(arg.parts)

    def _check_include(self, call: _Call) -> None:
        written = [self._written(arg.parts) for arg in call.args]
        if len(written) != 1 or not _QUOTED_PATH.fullmatch(written[0]):
            problem = (
                "takes the path of a template as a string, such as "
                f"'reports/ids.sql', got {', '.join(written)!r}"
            )
            raise self._refuse(INCLUDE, problem, call)

    def _render(self, parts: Sequence[_Part], ctx: Context) -> str:
        return "".join(
            [
                part if isinstance(part, str) else self._expanded(part, ctx)
                for part in parts
            ]
        )

    def _expanded(self, part: _Expansion | _FileExpansion, ctx: Context) -> str:
        """Write what a call writes, or, under `calls_kept`, the call it cannot."""
        try:
            if isinstance(part, _FileExpansion):
                return self._from_values(part, ctx)
            return str(self._call(part, ctx))
        except Exception:
            if not _calls_kept.get():
                raise
            call = part.call if isinstance(part, _FileExpansion) else part
            written = [
                self._render(parts or (), ctx) if param is None else self._shown(param)
                for param, parts in call.args
            ]
            return f"{self.namespace}.{call.macro.name}({', '.join(written)})"

    def _shown(self, param: str) -> str:
        """Write a parameter as the template does: `:a.b`, not the `:a__b` it binds as."""
        return ":" + ".".join(self.paths.get(param, (param,)))

    def _from_values(self, part: _FileExpansion, ctx: Context) -> str:
        """Call a `FileMacro` for its values, and write its SQL bound to them."""
        values = self._call(part.call, ctx)
        if not isinstance(values, Mapping):
            problem = (
                f"returned {type(values).__name__}, not the values {part.file} reads"
            )
            raise part.call.refuse(problem, self.namespace)
        missing = [name for name in part.reads if name not in values]
        extra = [name for name in values if name not in part.reads]
        problems = []
        if missing:
            problems.append(
                f"returned no {', '.join(missing)}, which {part.file} reads"
            )
        if extra:
            problems.append(
                f"returned {', '.join(extra)}, which {part.file} does not read"
            )
        if problems:
            raise part.call.refuse("; ".join(problems), self.namespace)
        for name in part.reads:
            ctx.values[f"{part.prefix}__{name}"] = values[name]
        return self._render(part.parts, ctx)

    def _call(self, call: _Expansion, ctx: Context) -> Any:  # noqa: ANN401
        """Call a macro with the arguments of the call, and return what it returns."""
        macro = call.macro
        values = ctx.values
        missing = [name for name in call.params if name not in values]
        if missing and not macro.optional:
            shown = ".".join(self.paths.get(missing[0], (missing[0],)))
            problem = f"`:{shown}` was not passed"
            raise call.refuse(problem, self.namespace)
        # Missing values read as None inside the arguments too, for the calls there.
        values.update(dict.fromkeys(missing))
        try:
            return macro.func(*self._arguments(call, ctx))
        except MacroArgumentError as error:
            if error.template:
                raise
            raise call.refuse(error.problem, self.namespace) from None
        finally:
            for name in missing:
                del values[name]

    def _refuse(
        self,
        macro: str,
        problem: str,
        call: _Call,
        span: tuple[int, int] | None = None,
    ) -> MacroArgumentError:
        return MacroArgumentError(
            macro,
            problem,
            self.name,
            call.line,
            namespace=self.namespace,
            chain=self.chain,
            span=span or call.span,
        )

    def _written(self, parts: Sequence[str | _Call]) -> str:
        return "".join(
            part if isinstance(part, str) else f"{self.namespace}.{part.name}(...)"
            for part in parts
        )

    def _arguments(self, call: _Expansion, ctx: Context) -> list[Any]:
        args: list[Any] = [ctx] if call.macro.context else []
        for param, parts in call.args:
            if param is not None and param in self.paths:
                args.append(_PathParam(param, ctx.values[param], self.paths[param]))
            elif param is not None:
                args.append(Param(param, ctx.values[param]))
            elif call.macro.lazy:
                args.append(_Deferred(self, parts or (), ctx))
            else:
                args.append(Sql(self._render(parts or (), ctx)))
        return args


@dataclass(frozen=True, slots=True)
class _FileExpansion:
    """A `FileMacro`'s call: the call for its values, and its SQL read in."""

    call: _Expansion
    prefix: str
    reads: tuple[str, ...]
    file: str
    parts: tuple[_Part, ...]


_Part: TypeAlias = "str | _Expansion | _FileExpansion"
"""A piece of a read template: text, a call to expand, or a `FileMacro`'s call."""


class _Deferred:
    """A branch's SQL, rendered the first time the macro reads it, and not before.

    `if_set(:x, a, b)` expands `a` or `b`, and a macro in the other one never
    runs, so it cannot fail on a value that was not meant for it.
    """

    __slots__ = ("_ctx", "_parts", "_template", "_text")

    def __init__(
        self,
        template: MacroTemplate,
        parts: Sequence[_Part],
        ctx: Context,
    ) -> None:
        self._template = template
        self._parts = parts
        self._ctx = ctx
        self._text: str | None = None

    def __str__(self) -> str:
        if self._text is None:
            self._text = self._template._render(self._parts, self._ctx)  # noqa: SLF001
        return self._text

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def strip(self) -> str:
        return str(self).strip()

    def head(self) -> str:
        """Return the text before the first macro's call, rendering nothing."""
        written = []
        for part in self._parts:
            if not isinstance(part, str):
                break
            written.append(part)
        return "".join(written)


@dataclass(frozen=True, slots=True)
class _Expansion:
    """A call resolved to its macro: each argument a parameter's name, or parts."""

    macro: Macro
    line: int
    args: tuple[tuple[str | None, tuple[_Part, ...] | None], ...]
    params: tuple[str, ...]
    template: str
    """The name of the template the call is written in."""
    chain: Chain

    def refuse(self, problem: str, namespace: str) -> MacroArgumentError:
        return MacroArgumentError(
            self.macro.name,
            problem,
            self.template,
            self.line,
            namespace=namespace,
            chain=self.chain,
        )


def _followed(value: Any, root: str, path: Sequence[str]) -> Any:  # noqa: ANN401
    """Return what `:root.path` reads: a key of a mapping, an attribute otherwise.

    A `None` on the way reads as `None` to the end, as an optional object
    that was not given: `:filters.team.id` with no team.

    Raises:
        ParameterPathError: if a step names nothing there.

    """
    read = [root]
    for step in path:
        if value is None:
            return None
        read.append(step)
        try:
            value = value[step] if isinstance(value, Mapping) else getattr(value, step)
        except (KeyError, AttributeError):
            raise ParameterPathError(".".join(read), step) from None
    return value


def _bracketed(parts: Sequence[str | _Call], index: int) -> bool:
    """Whether the call at ``index`` stands alone in brackets of its own."""
    before = parts[index - 1] if index else ""
    after = parts[index + 1] if index + 1 < len(parts) else ""
    return (
        isinstance(before, str)
        and isinstance(after, str)
        and before.rstrip().endswith("(")
        and after.lstrip().startswith(")")
    )


def _without_semicolon(source: str, scanner: _Scanner) -> str:
    """Return a query without the `;` that ends it, which a subquery cannot hold.

    The `;` is the last thing in the query that is not space or a comment:
    `SELECT 1; -- done` loses it too.
    """
    index, last = 0, -1
    while index < len(source):
        skipped = scanner.past_literal(index)
        if skipped == index:
            if not source[index].isspace():
                last = index
            index += 1
            continue
        if source[index] not in "-/":  # a string or a quoted name, not a comment
            last = skipped - 1
        index = skipped
    if last >= 0 and source[last] == ";":
        source = source[:last] + source[last + 1 :]
    return source.rstrip()


def _arity(macro: Macro) -> str:
    """Return how many arguments a macro takes, as a message says it."""
    if macro.variadic:
        return f"at least {macro.minimum}"
    if macro.minimum == macro.maximum:
        return str(macro.minimum)
    return f"{macro.minimum} to {int(macro.maximum)}"


_INCLUDED = 1024
"""How many included templates an engine keeps, one per chain it was read in."""


class MacroEngine:
    """Renders templates with `tpl.` macros, each file read once and kept."""

    def __init__(
        self,
        paths: Sequence[Path | str],
        macros: Mapping[str, Macro],
        *,
        auto_reload: bool = False,
        namespace: str = NAMESPACE,
    ) -> None:
        self.paths = tuple(Path(path) for path in paths)
        self.macros = macros
        self.auto_reload = auto_reload
        self.namespace = namespace
        self._loaded: dict[str, MacroTemplate] = {}
        self._included: dict[tuple[str, Chain], MacroTemplate] = {}
        self._from_string = lru_cache(maxsize=256)(
            lambda source: MacroTemplate(
                "<string>",
                source,
                self.macros,
                namespace=namespace,
                include=self.included,
            )
        )

    def render_file(
        self,
        name: str,
        context: Mapping[str, Any],
        preparer: Any,  # noqa: ANN401
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template file, and the values to bind to it."""
        return _rendered(self.get(name), context, preparer)

    def render_string(
        self,
        source: str,
        context: Mapping[str, Any],
        preparer: Any,  # noqa: ANN401
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template written out, and the values to bind to it."""
        return _rendered(self._from_string(source), context, preparer)

    def check(self, names: Iterable[str]) -> None:
        """Read these templates, which checks every call in them."""
        for name in names:
            self.get(name)

    def get(self, name: str) -> MacroTemplate:
        """Return a template by its name under the paths.

        Raises:
            TemplateNotFoundError: if no path holds it.

        """
        loaded = self._loaded.get(name)
        if loaded is not None and not (self.auto_reload and self._changed(loaded)):
            return loaded
        path = self._find(name)
        loaded = MacroTemplate(
            name,
            path.read_text(encoding="utf-8"),
            self.macros,
            path.stat().st_mtime,
            self.namespace,
            include=self.included,
        )
        self._loaded[name] = loaded
        return loaded

    def included(self, name: str, chain: Chain) -> MacroTemplate:
        """Return a template another includes, read once while no file changes.

        It is kept for the chain of includes it was read in, which its errors
        name. An editor reads the including text on each keystroke, and the
        included ones are read from here.

        Raises:
            TemplateNotFoundError: if no path holds it.

        """
        key = (name, chain)
        found = self._included.get(key)
        if found is not None and not self._changed(found):
            return found
        path = self._find(name)
        source = path.read_text(encoding="utf-8")
        found = MacroTemplate(
            name,
            _without_semicolon(source, _Scanner(source, name, self.namespace, chain)),
            self.macros,
            path.stat().st_mtime,
            self.namespace,
            include=self.included,
            chain=chain,
        )
        if len(self._included) >= _INCLUDED:
            del self._included[next(iter(self._included))]
        self._included[key] = found
        return found

    def _changed(self, template: MacroTemplate) -> bool:
        """Whether the file, or a file it includes, changed since it was read."""
        files = {template.name: template.mtime, **template.includes}
        try:
            return any(
                self._find(name).stat().st_mtime != mtime
                for name, mtime in files.items()
            )
        except TemplateNotFoundError:
            return True

    def _find(self, name: str) -> Path:
        pieces = name.split("/")
        if ".." in pieces or name.startswith("/"):
            raise TemplateNotFoundError(name, self.paths)
        if pieces[-1].endswith(MACRO_FILE):
            reason = f"a file whose name ends in `{MACRO_FILE}` holds macros"
            raise TemplateNotFoundError(name, self.paths, reason)
        for root in self.paths:
            path = root.joinpath(*pieces)
            if path.is_file():
                return path
        raise TemplateNotFoundError(name, self.paths)


def _rendered(
    template: MacroTemplate,
    context: Mapping[str, Any],
    preparer: Any,  # noqa: ANN401
) -> tuple[str, Mapping[str, Any]]:
    dialect = getattr(getattr(preparer, "dialect", None), "name", "")
    ctx = Context(dialect, preparer, context)
    return _as_bound(template.render(ctx), ctx.values, dialect), ctx.values


_IN_ONE = re.compile(r"\bIN\s*\(\s*:([A-Za-z_]\w*)\s*\)", re.IGNORECASE)
_LIMIT = re.compile(r"\b(LIMIT|OFFSET)(\s+):([A-Za-z_]\w*)\b", re.IGNORECASE)
_UNLIMITED = {
    "postgresql": "ALL",
    "snowflake": "NULL",
    "mysql": "18446744073709551615",
    "mariadb": "18446744073709551615",
    "sqlite": "-1",
}
"""What each database reads after `LIMIT` as every row."""


def _as_bound(sql: str, values: Mapping[str, Any], dialect: str) -> str:
    """Return the SQL with what a parameter's value decides written in.

    `IN (:ids)` is how a linter reads a list, and a list binds as one expanding
    parameter, which writes the brackets itself: `IN :ids`. A `LIMIT :limit`
    or an `OFFSET :offset` the call passes no value for, or `None`, takes every
    row and skips none, on the databases that read no `NULL` there.
    """

    def in_list(found: re.Match[str]) -> str:
        value = values.get(found.group(1))
        many = (
            value.expanding if isinstance(value, sa.BindParameter) else _expands(value)
        )
        return f"IN :{found.group(1)}" if many else found.group()

    def limit(found: re.Match[str]) -> str:
        clause, space, name = found.groups()
        if values.get(name) is not None:
            return found.group()
        written = "0" if clause.upper() == "OFFSET" else _UNLIMITED.get(dialect)
        return found.group() if written is None else f"{clause}{space}{written}"

    sql = _substituted(_LIMIT, limit, _substituted(_IN_ONE, in_list, sql))
    if not any(isinstance(value, Inline) for value in values.values()):
        return sql
    return _written_in(sql, values)


def _substituted(
    pattern: re.Pattern[str], write: Callable[[re.Match[str]], str], sql: str
) -> str:
    """Return `pattern.sub(write, sql)`, the pattern searched once for each SQL.

    A template renders the same SQL whenever its values have the same shape, and
    searching it is most of what `_as_bound` costs; SQL longer than
    `_CACHED_TEXT` is searched each time, as `_text` reads it.
    """
    found = (
        _found(pattern, sql)
        if len(sql) <= _CACHED_TEXT
        else tuple(pattern.finditer(sql))
    )
    if not found:
        return sql
    parts: list[str] = []
    done = 0
    for match in found:
        parts.extend((sql[done : match.start()], write(match)))
        done = match.end()
    parts.append(sql[done:])
    return "".join(parts)


@lru_cache(maxsize=1024)
def _found(pattern: re.Pattern[str], sql: str) -> tuple[re.Match[str], ...]:
    """Return each match of the pattern in the SQL, kept for `_substituted`."""
    return tuple(pattern.finditer(sql))


def _written_in(sql: str, values: Mapping[str, Any]) -> str:
    """Return the SQL with each `Inline` value written where its parameter stands.

    Raises:
        InlineValueError: for one where SQL takes a bound value instead.

    """
    written: list[str] = []
    done = 0
    for found in PARAMETER_IN_TEXT.finditer(sql):
        name = found.group("param")
        value = values.get(name) if name else None
        if not isinstance(value, Inline):
            continue
        if not inline_position(sql[: found.start()]):
            before = " ".join(sql[: found.start()].split()[-2:])
            raise InlineValueError(
                name,
                f"stands after `{before}`, where SQL takes a bound value: pass the "
                f"value itself, and it is bound",
            )
        written.extend((sql[done : found.start()], value.text))
        done = found.end()
    return "".join(written) + sql[done:]


PARAMETER_IN_TEXT = re.compile(
    rf"{_LITERAL}|(?<![:\w\\]):(?P<param>[A-Za-z_]\w*)", re.DOTALL
)
"""A `:parameter`, past the strings and comments that may hold a colon."""

_INLINE_AFTER = re.compile(
    r"(?:\b(?:INTO|FROM|JOIN|LIST|TABLE|VIEW|STAGE|TO|SCHEMA|DATABASE|USE)"
    r"|\bPUT\s+\S+"
    r"|\b(?:TABLE)?SAMPLE\s*(?:[A-Za-z_]+\s*)?\()\s*$",
    re.IGNORECASE,
)
"""Where SQL takes no bound value, only one written in: a stage, a name, a sample."""

STAGE_AFTER = re.compile(
    r"(?:\bCOPY\s+INTO\s+\S+\s+FROM|\bLIST|\bPUT\s+\S+)\s*$", re.IGNORECASE
)
"""Where a stage goes, which a linter reads only in its own shape: `@stage/path`."""

SAMPLE_AFTER = re.compile(
    r"\b(?:TABLE)?SAMPLE\s*(?:[A-Za-z_]+\s*)?\(\s*$", re.IGNORECASE
)
"""Where a sample's size goes, which a linter reads only as a number."""


def inline_position(before: str) -> bool:
    """Whether a parameter after this SQL stands where an `Inline` value may."""
    return _INLINE_AFTER.search(before) is not None


def registered(macros: Iterable[Macro | str | Path]) -> dict[str, Macro]:
    """Return the built-in macros and these, by name.

    A string is where to import them from: `app.sql.macros` for every macro of
    that module, `app.sql.macros:for_accounts` for one of them. A path to a
    `.sql` file reads the macros written in it.

    Raises:
        MacroDefinitionError: if two macros share a name, or a path names
            something that is not a macro.
        UnknownImportPathError: if a path names nothing that can be imported.

    """
    by_name = dict(BUILTIN_MACROS)
    files: set[Path] = set()
    unique = []
    for given in macros:
        if isinstance(given, Path) or (
            isinstance(given, str) and given.endswith(".sql")
        ):
            resolved = Path(given).resolve()
            if resolved in files:
                continue  # both listed in `macros=` and found in a directory
            files.add(resolved)
        unique.append(given)
    everything = [found for given in unique for found in _macros_given(given)]
    claimed = {
        (macro.name, Path(path).resolve())
        for macro in everything
        if (path := getattr(macro, "sql_path", None)) is not None
    }
    for macro in everything:
        if (
            isinstance(macro, SqlMacro)
            and (macro.name, macro.path.resolve()) in claimed
        ):
            continue  # claimed by a `FileMacro`
        if not isinstance(macro, Macro):
            raise MacroDefinitionError(
                getattr(macro, "__name__", repr(macro)),
                "it is not decorated @sql_macro",
            )
        if macro.name in by_name or macro.name == INCLUDE:
            raise MacroDefinitionError(macro.name, "another macro has that name")
        by_name[macro.name] = macro
    return by_name


def _macros_given(given: Macro | str | Path) -> list[Any]:
    """Return the macros one entry of `macros=` stands for."""
    if isinstance(given, Path) or (isinstance(given, str) and given.endswith(".sql")):
        return sql_macros(given)
    if isinstance(given, str):
        return _imported_macros(given)
    return [given]


def _imported_macros(path: str) -> list[Any]:
    """Return the macro a path names, or every macro of the module it names."""
    if ":" not in path and _is_module(path):
        module = importlib.import_module(path)
        return [value for value in vars(module).values() if isinstance(value, Macro)]
    return [import_string(path)]


def _is_module(path: str) -> bool:
    try:
        return importlib.util.find_spec(path) is not None
    except ModuleNotFoundError:  # a parent that is a module, not a package
        return False


@sql_macro(optional=True, lazy=True)
def if_set(value: Param, expr: Sql, otherwise: Sql = Sql("TRUE")) -> str:  # noqa: B008
    """`expr` when the parameter holds a value, `otherwise` when it does not.

    `None`, an empty string, an empty list and `False` hold none, and so does a
    parameter the call did not pass. `otherwise` is `TRUE`, which leaves a
    `WHERE` or an `AND` as though the condition were not there.

    A branch that joins conditions with `AND` or `OR` goes in brackets, so it
    stays one condition next to another: `x AND tpl.if_set(:y, a OR b)` writes
    `x AND (a OR b)`. Any other branch goes in as written, a column or a sort
    term included.
    """
    return _grouped(str(expr if _is_set(value.value) else otherwise))


@sql_macro(optional=True, lazy=True)
def unless_set(value: Param, expr: Sql, otherwise: Sql = Sql("TRUE")) -> str:  # noqa: B008
    """`expr` when the parameter holds no value, `otherwise` when it does.

    The other way round from `if_set`, for what applies only when a filter is
    not given: `AND tpl.unless_set(:status, status <> 'archived')`. A branch
    with `AND` or `OR` goes in brackets, as in `if_set`.
    """
    return _grouped(str(otherwise if _is_set(value.value) else expr))


@sql_macro(optional=True, lazy=True)
def if_not_set(value: Param, expr: Sql, otherwise: Sql = Sql("TRUE")) -> str:  # noqa: B008
    """`expr` when the parameter holds no value: another name for `unless_set`.

    It reads as the other way round from `if_set`:
    `AND tpl.if_not_set(:status, status <> 'archived')`.
    """
    return _grouped(str(otherwise if _is_set(value.value) else expr))


@sql_macro(optional=True)
def array(ctx: Context, values: Param, type_name: Sql = Sql("")) -> str:  # noqa: B008
    """Write a list as an array, one parameter per value.

    ```sql
    WHERE genres && tpl.array(:genres, 'text')
    ```

    `ARRAY[...]` on PostgreSQL, cast to the type when one is given:
    `ARRAY[:genres__1, :genres__2]::text[]`, since the driver binds a string
    as `varchar`, and `varchar[] && text[]` does not exist. An empty array
    needs the type there. `ARRAY_CONSTRUCT(...)` on Snowflake, which has one
    array type. A value that is not there writes `NULL`.

    Raises:
        MacroArgumentError: on another database, for a type that is not a type
            name, or for an empty array with no type on PostgreSQL.

    """
    _for_dialect(ctx, array.name, postgresql=True, snowflake=True)
    written = type_name.strip().strip("'")
    if written and not _TYPE_NAME.fullmatch(written):
        problem = (
            f"takes a type name such as 'text' or 'integer', got {type_name.strip()}"
        )
        raise MacroArgumentError(array.name, problem)
    items = values.value
    if isinstance(items, str | bytes):
        problem = f"`{_as_written(values)}` is a string, and takes a list"
        raise MacroArgumentError(array.name, problem)
    if items is None:
        bound = None
    else:
        bound = [ctx.bind(item, _named(values)) for item in items]
    if ctx.dialect == "snowflake":
        return "NULL" if bound is None else f"ARRAY_CONSTRUCT({', '.join(bound)})"
    cast = f"::{written}[]" if written else ""
    if bound is None:
        return f"NULL{cast}"
    if not bound and not cast:
        problem = (
            f"`{_as_written(values)}` is empty, and PostgreSQL needs its type: pass one"
        )
        raise MacroArgumentError(array.name, problem)
    return f"ARRAY[{', '.join(bound)}]{cast}"


_JOINS_CONDITIONS = re.compile(r"\b(AND|OR)\b", re.IGNORECASE)
_CASE = re.compile(r"(?<!\w)CASE\b", re.IGNORECASE)
_END = re.compile(r"(?<!\w)END\b", re.IGNORECASE)
_NAMED_OR_SORTED = re.compile(
    r"(?:\bAS\s+[\w\"`]+|\b(?:ASC|DESC))(?:\s+NULLS\s+(?:FIRST|LAST))?\s*$",
    re.IGNORECASE,
)


@lru_cache(maxsize=1024)
def _grouped(sql: str) -> str:
    """Return a condition in brackets when it joins others with `AND` or `OR`.

    Only a join outside brackets, `CASE ... END` and strings counts: `(a OR b)`
    and `'a or b'` stay as they are. A column named with `AS`, or a sort term
    ending in `ASC` or `DESC`, is no condition, and stays too. A branch holds
    placeholders, not values, so the texts it sees are few, and each is read
    once.
    """
    if not _JOINS_CONDITIONS.search(sql) or _NAMED_OR_SORTED.search(sql):
        return sql
    scanner = _Scanner(sql, "", NAMESPACE)
    depth = index = 0
    while index < len(sql):
        skipped = scanner.past_literal(index)
        if skipped != index:
            index = skipped
            continue
        char = sql[index]
        if char in "([{" or _CASE.match(sql, index):
            depth += 1
        elif char in ")]}" or _END.match(sql, index):
            depth -= 1
        elif depth == 0 and _JOINS_CONDITIONS.match(sql, index):
            return f"({sql})"
        index += 1
    return sql


def _is_set(value: Any) -> bool:  # noqa: ANN401
    """Whether a value is there: not None, not False, and not empty. `0` is there."""
    if value is None or value is False:
        return False
    return not isinstance(value, Sized) or len(value) > 0


@sql_macro(optional=True)
def order_by(ctx: Context, sort: Param, column: Sql, *columns: Sql) -> str:
    """`ORDER BY` terms from sort strings: `name`, `name.desc`, `name.desc.nulls_last`.

    One string or a list of them, sorting only by the columns listed after the
    parameter: the names come from a request, and nothing else reaches the SQL.
    A sort string names a column by its last part, `u.name` as `name`, and
    `name = <expression>` sorts by something else under that name:

    ```sql
    ORDER BY tpl.order_by(:sort, id, name = tpl.icollate(name), 'name.asc', 'nulls_last')
    ```

    A string in quotes is an option. `'nulls_last'` or `'nulls_first'` places
    the nulls of every term whose sort string does not say, and MySQL, which
    has no `NULLS LAST`, sorts by `IS NULL` first. Any other string is the sort
    when the call passes none, `'name.asc'` above. With neither, the rows come
    in no order: `NULL`, or `(SELECT NULL)` on PostgreSQL and SQL Server, which
    refuse a bare `NULL`.
    """
    written = [one.strip() for one in (column, *columns)]
    options = [one.strip("'") for one in written if one.startswith("'")]
    nulls_options = [one.lower() for one in options if one.lower() in _NULLS_OPTIONS]
    default_nulls = nulls_options[-1] if nulls_options else None
    default_sort = [one for one in options if one.lower() not in _NULLS_OPTIONS]
    offered = _offered(one for one in written if not one.startswith("'"))
    requested = sort.value or default_sort
    if not requested:
        return ctx.no_order
    fields = [requested] if isinstance(requested, str) else list(requested)
    terms = []
    for field in fields:
        name, descending, nulls = _parse_sort_field(str(field))
        direction = str(field).split(".")[1:2]
        if [one.lower() for one in direction] not in ([], ["asc"], ["desc"]) or (
            nulls not in (None, "nulls_first", "nulls_last")
        ):
            raise InvalidSortStringError(str(field))
        expression = offered[_field_named(name, offered)]
        nulls = nulls or default_nulls
        terms.append(
            _sort_term(ctx.dialect, expression, descending=descending, nulls=nulls)
        )
    return ", ".join(terms) or ctx.no_order


_NULLS_OPTIONS = ("nulls_first", "nulls_last")


def _sort_term(
    dialect: str, expression: str, *, descending: bool, nulls: str | None
) -> str:
    """Return one `ORDER BY` term, with its nulls placed the way the dialect can."""
    term = f"{expression} {'DESC' if descending else 'ASC'}"
    if nulls is None:
        return term
    last = nulls == "nulls_last"
    if dialect in ("mysql", "mariadb"):
        return f"{expression} IS NULL {'ASC' if last else 'DESC'}, {term}"
    return f"{term} NULLS {'LAST' if last else 'FIRST'}"


def _offered(columns: Iterable[str]) -> dict[str, str]:
    """Return the columns a template lists, by the name a request asks for each.

    `u.name` is asked for as `name`, and `name = <expression>` is the expression
    under that name.
    """
    offered = {}
    for column in columns:
        if named := _COLUMN_AS.fullmatch(column):
            offered[named.group(1)] = named.group(2)
        else:
            offered[_last_name(column)] = column.strip()
    return offered


def _last_name(column: str) -> str:
    """Return the name a column is asked for by: `u.name` is `name`."""
    return column.strip().rsplit(".", 1)[-1].strip('"`[]')


@sql_macro
def icontains(
    ctx: Context,
    column: Sql,
    text: Sql,
    collation: Sql = Sql("'en-ci'"),  # noqa: B008
) -> str:
    """Whether a column holds the text anywhere, regardless of case.

    The text is a parameter or any expression: `icontains(city, spaced(:name))`.
    `ILIKE` on PostgreSQL, `CONTAINS(COLLATE(...))` on Snowflake, and `lower()`
    on both sides with `LIKE` elsewhere. `%` and `_` in the text match only
    themselves: the database escapes them before it compares. ``collation`` is
    the one Snowflake compares under, `'en-ci-ai'` to ignore accents as well;
    the other databases ignore it.
    """
    if ctx.dialect == "snowflake":
        return f"CONTAINS(COLLATE({column}, {collation}), {text})"
    param = _PARAMETER.fullmatch(text)
    if param and param.group(1) in ctx.values:
        # A plain parameter: escape its value here, and bind the pattern.
        if ctx.values[param.group(1)] is None:
            return "FALSE"  # `None` holds no text, as `LIKE NULL` finds none
        value = str(ctx.values[param.group(1)])
        escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        pattern = ctx.bind(f"%{escaped}%", f"{param.group(1)}__like")
    elif ctx.dialect in ("mysql", "mariadb"):
        pattern = f"CONCAT('%', {_escaped_like(text)}, '%')"
    else:
        pattern = f"'%' || {_escaped_like(text)} || '%'"
    if ctx.dialect == "postgresql":
        return f"{column} ILIKE {pattern} ESCAPE '!'"
    return f"lower({column}) LIKE lower({pattern}) ESCAPE '!'"


def _escaped_like(text: str) -> str:
    """Return SQL that escapes `!`, `%` and `_` in the text, for `LIKE ... ESCAPE '!'`."""
    return f"replace(replace(replace({text}, '!', '!!'), '%', '!%'), '_', '!_')"


@sql_macro
def icollate(
    ctx: Context,
    column: Sql,
    collation: Sql = Sql("'en-ci'"),  # noqa: B008
) -> str:
    """Write a column so that it compares and sorts without regard to case.

    ```sql
    ORDER BY tpl.order_by(:sort, id, name = tpl.icollate(name), 'nulls_last')
    WHERE tpl.icollate(email) = tpl.icollate(:email)
    ```

    `COLLATE(column, ...)` on Snowflake, under ``collation``: `'en-ci'` unless it says
    `'en-ci-ai'` to ignore accents as well. `lower()` elsewhere, which minds
    accents, and the column as it is on MySQL, which compares without regard
    to case already.

    In a `GROUP BY` or a `DISTINCT` it changes more than the order: `COLLATE`
    groups `A` with `a` on Snowflake, and `lower()` returns `a` for both.
    """
    if ctx.dialect == "snowflake":
        return f"COLLATE({column}, {collation})"
    if ctx.dialect in ("mysql", "mariadb"):
        return column
    return f"lower({column})"


@sql_macro(optional=True)
def between(
    column: Sql,
    start: Param,
    end: Param,
    bounds: Literal["'[]'", "'[)'"] = "'[]'",
) -> str:
    """Keep a column within a range whose ends may each be missing.

    ```sql
    AND tpl.between(o.created_on, :date_from, :date_to)
    AND tpl.between(created_at, :since, :until, '[)')
    ```

    `BETWEEN` when both ends are there, `>=` or `<=` when one is, `TRUE` when
    neither is: an end holds no value as in `if_set`. ``bounds`` is `'[]'`,
    which includes the end as `BETWEEN` does, or `'[)'`, which leaves it out,
    as a range of times wants. A start after the end matches no rows.
    """
    has_start, has_end = _is_set(start.value), _is_set(end.value)
    closed = bounds == "'[]'"
    below = "<=" if closed else "<"
    if has_start and has_end:
        if closed:
            return f"{column} BETWEEN {start} AND {end}"
        return f"({column} >= {start} AND {column} < {end})"
    if has_start:
        return f"{column} >= {start}"
    if has_end:
        return f"{column} {below} {end}"
    return "TRUE"


@sql_macro(optional=True)
def in_list(column: Sql, values: Param, exclude: Param) -> str:
    """Match the column against a list, or everything but it, as a flag says.

    ```sql
    WHERE tpl.in_list(o.status, :statuses, :exclude_statuses)
    ```

    `column IN (:values)`, or `column NOT IN (:values)` when ``exclude`` holds
    a value. `TRUE` when the list holds none: no filter either way.
    """
    if not _is_set(values.value):
        return "TRUE"
    negated = "NOT " if _is_set(exclude.value) else ""
    return f"{column} {negated}IN ({values})"


@sql_macro
def arrays_overlap(ctx: Context, array: Sql, other: Sql) -> str:
    """Whether two arrays hold a value in common.

    `a && b` on PostgreSQL, and `ARRAYS_OVERLAP(a, b)` on Snowflake.

    Raises:
        MacroArgumentError: on another database, which has no arrays.

    """
    _for_dialect(ctx, arrays_overlap.name, postgresql=True, snowflake=True)
    if ctx.dialect == "snowflake":
        return f"ARRAYS_OVERLAP({array}, {other})"
    return f"({array} && {other})"


@sql_macro
def array_contains_all(ctx: Context, array: Sql, other: Sql) -> str:
    """Whether the first array holds every value of the second.

    `a @> b` on PostgreSQL. On Snowflake, the second array has nothing the
    first lacks: `ARRAY_SIZE(ARRAY_EXCEPT(b, a)) = 0`.

    Raises:
        MacroArgumentError: on another database, which has no arrays.

    """
    _for_dialect(ctx, array_contains_all.name, postgresql=True, snowflake=True)
    if ctx.dialect == "snowflake":
        return f"(ARRAY_SIZE(ARRAY_EXCEPT({other}, {array})) = 0)"
    return f"({array} @> {other})"


@sql_macro(optional=True)
def any_of(ctx: Context, rows: Param, row: Sql, condition: Sql) -> str:
    """Whether the condition holds for any of the rows, `row.field` reading a field.

    ```sql
    WHERE tpl.any_of(
        :cities, city,
        ST_DISTANCE(p.location, ST_POINT(city.lon, city.lat)) <= city.radius
    )
    ```

    The condition is written once for each row, its `city.field` bound to the
    row's field, and the conditions joined with `OR`. A row is an object or a
    mapping. No rows, or none passed, is `FALSE`: wrap the call in
    `tpl.if_set(:cities, ...)` for a filter that is off when nothing is passed.

    The SQL grows with the rows, so this is for a few dozen. A condition made
    of equalities only is better as `EXISTS (SELECT 1 FROM tpl.values(:rows) AS
    v WHERE t.a = v.column1)`, which the database joins. Snowflake evaluates no
    subquery tied to the outer row by anything else, a distance, a range or a
    `LIKE`, and this macro covers those.

    Raises:
        MacroArgumentError: if ``row`` is not a name, or a row has no field the
            condition reads.

    """
    name = str(row).strip()
    if not _IDENTIFIER.fullmatch(name):
        problem = f"`{name}` is not a name to read the fields of a row under"
        raise MacroArgumentError(any_of.name, problem)
    field = re.compile(
        rf"{_LITERAL}|(?<![\w.]){re.escape(name)}\.(?P<field>[A-Za-z_]\w*)\b",
        re.DOTALL,
    )
    prefix = _named(rows)

    def written(item: Any, found: re.Match[str]) -> str:  # noqa: ANN401
        key = found.group("field")
        if key is None:
            return found.group()
        try:
            value = item[key] if isinstance(item, Mapping) else getattr(item, key)
        except (KeyError, AttributeError):
            problem = f"a row of `{_as_written(rows)}` has no field `{key}`"
            raise MacroArgumentError(any_of.name, problem) from None
        return ctx.bind(value, f"{prefix}_{key}" if prefix else key)

    text = str(condition)
    # A `--` comment at the end would take the `)` and the `OR` after it along.
    if any(
        found.group().startswith("--") and found.end() == len(text.rstrip())
        for found in re.finditer(_LITERAL, text, re.DOTALL)
    ):
        text = text.rstrip() + "\n"
    conditions = [
        _grouped(field.sub(partial(written, item), text)) for item in rows.value or ()
    ]
    if not conditions:
        return "FALSE"
    return _grouped(" OR ".join(conditions))


@sql_macro(name="values")
def values_table(ctx: Context, rows: Param) -> str:
    """Write a small table out in the query, one parameter per value.

    ```sql
    SELECT column1 AS position, column2 AS name FROM tpl.values(:rows) AS v
    ```

    Rows are tuples or lists of one length; a plain value is a row of one
    column. The columns are `column1`, `column2`, ... on every database. MySQL
    and MariaDB name the columns of `VALUES` otherwise, and Oracle wants them
    named, so there it is written as `SELECT ... UNION ALL SELECT ...`, from
    `dual` on Oracle. Name them in the select list, since
    MariaDB takes no `AS v (position, name)`. The database works out the types,
    and PostgreSQL wants one type down a column: cast where the rows mix them.

    For a few dozen rows. Hundreds are better sent as one JSON value and
    unpacked in the database, with `FLATTEN` or `json_array_elements`. In an
    `EXISTS` tied to the outer row, compare by equalities only: Snowflake
    refuses any other tie, and `any_of` writes that condition for each row.

    Raises:
        MacroArgumentError: if there are no rows, or they differ in length.

    """
    table = [
        tuple(row) if isinstance(row, tuple | list) else (row,)
        for row in rows.value or ()
    ]
    if not table:
        problem = (
            f"`{_as_written(rows)}` has no rows, and `VALUES` without one is not SQL"
        )
        raise MacroArgumentError(values_table.name, problem)
    width = len(table[0])
    for number, row in enumerate(table, 1):
        if len(row) != width:
            problem = (
                f"row {number} of `{_as_written(rows)}` has {len(row)} values, "
                f"and row 1 has {width}"
            )
            raise MacroArgumentError(values_table.name, problem)
    name = _named(rows)
    bound = [[ctx.bind(value, name) for value in row] for row in table]
    if ctx.dialect in ("mysql", "mariadb", "oracle"):
        first, *rest = bound
        named = ", ".join(
            f"{value} AS column{index}" for index, value in enumerate(first, 1)
        )
        # Oracle selects from something, and `dual` is the table of one row.
        source = " FROM dual" if ctx.dialect == "oracle" else ""
        selects = [
            f"SELECT {named}{source}",
            *(f"SELECT {', '.join(row)}{source}" for row in rest),
        ]
        return f"({' UNION ALL '.join(selects)})"
    written = ", ".join(f"({', '.join(row)})" for row in bound)
    return f"(VALUES {written})"


@sql_macro
def json_object(ctx: Context, *pairs: Sql) -> str:
    """Build a JSON object of keys and values: `json_object('id', id, 'name', name)`.

    `JSON_BUILD_OBJECT` on PostgreSQL, `OBJECT_CONSTRUCT_KEEP_NULL` on
    Snowflake, `JSON_OBJECT` on MySQL, MariaDB and SQLite, and `JSON_OBJECT`
    with `key VALUE value` pairs on Oracle. A key whose value is `NULL` stays in
    the object on every one of them.

    Raises:
        MacroArgumentError: on another database.

    """
    name = _for_dialect(
        ctx,
        json_object.name,
        postgresql="JSON_BUILD_OBJECT",
        snowflake="OBJECT_CONSTRUCT_KEEP_NULL",
        mysql="JSON_OBJECT",
        mariadb="JSON_OBJECT",
        sqlite="json_object",
        oracle="JSON_OBJECT",
    )
    if len(pairs) % 2:
        problem = f"takes keys and values in pairs, got {len(pairs)} arguments"
        raise MacroArgumentError(json_object.name, problem)
    if ctx.dialect == "oracle":
        keyed = [
            f"{key} VALUE {value}"
            for key, value in zip(*[iter(pairs)] * 2, strict=True)
        ]
        return f"{name}({', '.join(keyed)} NULL ON NULL)"
    return f"{name}({', '.join(pairs)})"


@sql_macro
def array_agg(ctx: Context, value: Sql, *order_by: Sql) -> str:
    """Gather the values of a group into an array, in the order the rest name.

    `ARRAY_AGG(value ORDER BY ...)` on PostgreSQL, and `ARRAY_AGG(value)
    WITHIN GROUP (ORDER BY ...)` on Snowflake.

    Raises:
        MacroArgumentError: on another database, which has no arrays.

    """
    _for_dialect(ctx, array_agg.name, postgresql=True, snowflake=True)
    return _aggregate(ctx, "ARRAY_AGG", [value], order_by)


@sql_macro
def string_agg(ctx: Context, value: Sql, separator: Sql, *order_by: Sql) -> str:
    """Join the values of a group with the separator, in the order the rest name.

    `STRING_AGG` on PostgreSQL, `LISTAGG ... WITHIN GROUP` on Snowflake and
    Oracle, `GROUP_CONCAT ... SEPARATOR` on MySQL and MariaDB, and
    `group_concat` on SQLite, which takes an order from 3.44 on.

    Raises:
        MacroArgumentError: on another database.

    """
    _for_dialect(
        ctx,
        string_agg.name,
        postgresql=True,
        snowflake=True,
        mysql=True,
        mariadb=True,
        sqlite=True,
        oracle=True,
    )
    order = f" ORDER BY {', '.join(order_by)}" if order_by else ""
    if ctx.dialect in ("mysql", "mariadb"):
        return f"GROUP_CONCAT({value}{order} SEPARATOR {separator})"
    if ctx.dialect in ("snowflake", "oracle"):
        return _aggregate(ctx, "LISTAGG", [value, separator], order_by)
    name = "group_concat" if ctx.dialect == "sqlite" else "STRING_AGG"
    return f"{name}({value}, {separator}{order})"


def _aggregate(
    ctx: Context, name: str, args: Sequence[str], order_by: Sequence[str]
) -> str:
    """Write an ordered aggregate: the order inside it, or `WITHIN GROUP` after."""
    written = ", ".join(args)
    if not order_by:
        return f"{name}({written})"
    order = f"ORDER BY {', '.join(order_by)}"
    if ctx.dialect in ("snowflake", "oracle"):
        return f"{name}({written}) WITHIN GROUP ({order})"
    return f"{name}({written} {order})"


@sql_macro
def array_contains(ctx: Context, array: Sql, value: Sql) -> str:
    """Whether an array holds the value.

    `value = ANY(array)` on PostgreSQL, and `ARRAY_CONTAINS(value::variant,
    array)` on Snowflake.

    Raises:
        MacroArgumentError: on another database, which has no arrays.

    """
    _for_dialect(ctx, array_contains.name, postgresql=True, snowflake=True)
    if ctx.dialect == "snowflake":
        return f"ARRAY_CONTAINS({value}::variant, {array})"
    return f"{value} = ANY({array})"


@sql_macro(lazy=True)
def on_dialect(ctx: Context, branch: Sql, *branches: Sql) -> str:
    """Write the SQL of the database in hand: `postgresql = a, snowflake = b`.

    ```sql
    FROM tpl.on_dialect(postgresql = countries, snowflake = analytics.prod.countries)
    ```

    `default = ...` is for every database not named. Prefer a macro that names
    the difference, such as `icontains`. This is for what none covers, such as
    a table that lives elsewhere.

    Raises:
        MacroArgumentError: if a branch is not `name = sql`, or none is for the
            database in hand.

    """
    # Only the branch for the database in hand renders, so a macro in another,
    # one this database has no form for, never runs.
    named: dict[str, Sql | _Deferred] = {}
    for one in (branch, *branches):
        head = one.head() if isinstance(one, _Deferred) else str(one)
        found = _BRANCH_NAMED.match(head)
        if found is None:
            problem = f"takes `dialect = sql` branches, got {head.strip()!r}"
            raise MacroArgumentError(on_dialect.name, problem)
        named[found.group(1)] = one
    chosen = named.get(ctx.dialect, named.get("default"))
    if chosen is None:
        problem = (
            f"has no branch for {ctx.dialect}, and no `default = ...`: "
            f"it has {', '.join(named)}"
        )
        raise MacroArgumentError(on_dialect.name, problem)
    written = _COLUMN_AS.fullmatch(str(chosen))
    return written.group(2) if written is not None else str(chosen)


def _for_dialect(ctx: Context, macro: str, **forms: Any) -> Any:  # noqa: ANN401
    """Return what the database in hand writes, or say the macro has no form for it.

    Raises:
        MacroArgumentError: if the dialect is none of them.

    """
    if ctx.dialect not in forms:
        problem = f"has no form for {ctx.dialect}, only for {', '.join(forms)}"
        raise MacroArgumentError(macro, problem)
    return forms[ctx.dialect]


@sql_macro
def identifier(ctx: Context, name: Param, *allowed: Sql) -> str:
    """Write a name from a parameter, quoted the way this database quotes one.

    A name that needs no quoting is left alone: `name` on PostgreSQL, where
    `Mixed Name` becomes `"Mixed Name"`. A tuple or a list is a qualified name,
    `("reports", "events")` for `reports.events`.

    With names listed after the parameter, only those are taken, matched the way
    `order_by` matches a sort string, and written as the template lists them:

    ```sql
    SELECT tpl.identifier(:column, id, name, created = created_at) FROM users
    ```

    Pass the list for a name that comes from a request: quoting stops
    injection, not an unknown column.
    """
    value = name.value
    if allowed:
        offered = _offered(allowed)
        if not isinstance(value, str) or not value:
            raise UnknownIdentifierError(value, offered)
        try:
            return offered[_field_named(value, offered)]
        except UnknownOrderFieldError:
            raise UnknownIdentifierError(value, offered) from None
    if isinstance(value, Inline):
        raise UnknownIdentifierError(value.text)
    parts = (value,) if isinstance(value, str) else tuple(value or ())
    if not parts or not all(isinstance(part, str) and part for part in parts):
        raise UnknownIdentifierError(value)
    # A colon in a quoted name is the name's, and not a parameter to bind.
    return ".".join(ctx.quote(part) for part in parts).replace(":", "\\:")


@sql_macro
def each(ctx: Context, values: Param) -> str:
    """Write each value of a list as a parameter of its own, separated by commas.

    For a list where `IN :ids` cannot go: `ARRAY[tpl.each(:ids)]`, or the
    arguments of a function. SQLAlchemy writes an expanding parameter in
    parentheses, so `ARRAY[:ids]` holds one row of the values, not the values.
    `IN :ids` stays the way to match a list: its SQL is the same whatever the
    length.

    Raises:
        MacroArgumentError: if the list is empty: `IN ()` is not SQL.

    """
    if isinstance(values.value, str | bytes):
        problem = f"`{_as_written(values)}` is a string, and takes a list"
        raise MacroArgumentError(each.name, problem)
    items = list(values.value or ())
    if not items:
        problem = f"`{_as_written(values)}` is empty, and `IN ()` is not SQL"
        raise MacroArgumentError(each.name, problem)
    return ", ".join(ctx.bind(item, _named(values)) for item in items)


BUILTIN_MACROS: Mapping[str, Macro] = {
    macro.name: macro
    for macro in (
        if_set,
        unless_set,
        if_not_set,
        order_by,
        icontains,
        icollate,
        json_object,
        array_agg,
        string_agg,
        array_contains,
        on_dialect,
        between,
        identifier,
        each,
        in_list,
        array,
        arrays_overlap,
        array_contains_all,
        values_table,
        any_of,
    )
}

tpl = SimpleNamespace(**BUILTIN_MACROS)
"""The built-in macros, to call from a macro of your own as a template would.

```python
@sql_macro
def search(q: Param, *columns: Sql) -> str:
    if not q.value:
        return "TRUE"
    return " OR ".join(tpl.icontains(column, q) for column in columns)
```
"""


class Templates:
    """The directory a database's SQL templates live in, and how they render.

    A path is enough; the object is for the rest:

    ```python
    db = Database(DB_URL, templates=Templates("app/sql", auto_reload=DEBUG))
    ```

    ``auto_reload`` reads a template again when its file changes, which a
    development server wants and a production one does not.

    A template is SQL with `:name` parameters and `tpl.<macro>(...)` calls.
    ``macros`` are the ones an application adds to the built-in ones, which
    `sqlakit macros` lists:

    ```python
    Templates("app/sql", macros=[for_accounts])
    Templates("app/sql", macros=["app.sql.macros"])  # every macro of a module
    ```

    ``namespace`` is the schema name calls are written under, `tpl` unless a
    real schema has that name: `Templates("app/sql", namespace="q")` reads
    `q.if_set(...)`.
    """

    def __init__(
        self,
        path: PathLike | Sequence[PathLike] = (),
        *,
        auto_reload: bool = False,
        macros: Iterable[Macro | str | Path] = (),
        namespace: str = NAMESPACE,
    ) -> None:
        self.paths = (
            (path,) if isinstance(path, str | Path) else tuple(path)  # type: ignore[misc]
        )
        self.auto_reload = auto_reload
        if not _IDENTIFIER.fullmatch(namespace):
            msg = f"`namespace` is a plain name, such as `tpl`, not {namespace!r}"
            raise ValueError(msg)
        self.namespace = namespace
        self.macros = registered([*macros, *self._macro_files()])

    def __repr__(self) -> str:
        paths = ", ".join(str(path) for path in self.paths)
        return f"{type(self).__name__}({paths!r})"

    def _macro_files(self) -> list[Path]:
        """Return the files of SQL macros in the template directories.

        A file is one when its name ends in `macros.sql`: `_macros.sql`,
        `tenant.macros.sql`. These are registered without being listed in
        `macros`.
        """
        return sorted(
            path
            for root in map(Path, self.paths)
            if root.is_dir()
            for path in root.rglob(f"*{MACRO_FILE}")
        )

    @cached_property
    def engine(self) -> MacroEngine:
        """The engine that reads and renders the templates."""
        return MacroEngine(
            self.paths,
            self.macros,
            auto_reload=self.auto_reload,
            namespace=self.namespace,
        )

    def render(
        self,
        source: str,
        context: Mapping[str, Any],
        *,
        preparer: Any,  # noqa: ANN401 - SQLAlchemy's IdentifierPreparer
        inline: bool = False,
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template, and the values to bind to it.

        Synchronous in both APIs: it reads a compiled template and builds a string.

        Raises:
            SQLNotConfiguredError: if a file is asked for and no path was given.
            TemplateNotFoundError: if no path holds that template.

        """
        if inline:
            return self.engine.render_string(source, context, preparer)
        if not self.paths:
            raise SQLNotConfiguredError
        return self.engine.render_file(source, context, preparer)

    def names(self) -> list[str]:
        """Return the name of every template: each `.sql` file under the paths.

        A file of SQL macros is not one, wherever it lives.
        """
        macro_files = {
            Path(path).resolve()
            for macro in self.macros.values()
            if (
                path := getattr(macro, "sql_path", None)
                or (macro.path if isinstance(macro, SqlMacro) else None)
            )
        }
        found = {
            path.relative_to(root).as_posix()
            for root in (Path(path).resolve() for path in self.paths)
            for path in root.rglob("*.sql")
            if path not in macro_files
        }
        return sorted(found)

    def check_macro(
        self, macro: SqlMacro, macros: Mapping[str, Macro] | None = None
    ) -> None:
        """Read an SQL macro's body the way a template calling it would.

        Each argument stands as a parameter the calling template passes, which
        goes wherever a macro in the body takes one. ``macros`` are the ones
        the body may call, the registered ones unless an editor holds others.

        Raises:
            MacroSyntaxError: if the body cannot be read.
            UnknownMacroError: if it calls a macro nobody registered.
            MacroArgumentError: if a call has arguments its macro cannot take.

        """
        MacroTemplate(
            macro.source_name,
            macro.expanded([f":{param}" for param in macro.params]),
            self.macros if macros is None else macros,
            namespace=self.namespace,
            include=self.engine.included,
            expanding=(macro.name,),
            first_line=macro.body_line,
        )

    def check(self) -> None:
        """Read every `.sql` template, so a broken one fails where deploys do.

        Raises:
            SQLNotConfiguredError: if no template path was given.
            MacroSyntaxError: if a template cannot be read.
            UnknownMacroError: if it calls a macro nobody registered.
            MacroArgumentError: if a call has arguments its macro cannot take.

        """
        if not self.paths:
            raise SQLNotConfiguredError
        self.engine.check(self.names())
        for macro in self.macros.values():
            if isinstance(macro, SqlMacro):
                self.check_macro(macro)


class BaseSQLQuery(Generic[RowT, DatabaseT]):
    """The source of the SQL, its context, and the type its rows become.

    Built by `db.sql(...)`. The SQL alone decides the rows.
    """

    def __init__(  # noqa: PLR0913 - the shape of a query, not a call site
        self,
        db: DatabaseT,
        source: str | Executable,
        context: Mapping[str, Any],
        *,
        inline: bool = False,
        type_: type[Any] | None = None,
        scalar: bool = False,
        validation: Mapping[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.source = source
        self.context = context
        self.inline = inline
        self.type = type_
        self.scalar = scalar
        self.validation = dict(validation or {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.source!r})"

    @cached_property
    def statement(self) -> Executable:
        """The SQL this runs, rendered and bound.

        A test asserts on it, and `EXPLAIN` takes it. A statement handed over ready
        is itself.
        """
        if not isinstance(self.source, str):
            return self.source
        dialect = self.db.engine.dialect
        context = {"dialect": dialect.name, **self.context}
        sql, params = templates_of(self.db).render(
            self.source,
            context,
            preparer=dialect.identifier_preparer,
            inline=self.inline,
        )
        return _statement(sql, params, label=None if self.inline else self.source)

    def __clause_element__(self) -> Executable:
        """Stand in for the statement wherever SQLAlchemy expects one.

        ```python
        User.query.from_statement(db.sql("users/active.sql", team="red")).all()
        ```
        """
        return self.statement

    def _as(self, query: type[QueryT], **changes: Any) -> QueryT:  # noqa: ANN401
        """Return the same template read another way, as another class.

        Each class offers only the methods valid for its state, so `typed()`
        cannot be called on rows that carry a type already.
        """
        arguments = {
            "inline": self.inline,
            "type_": self.type,
            "scalar": self.scalar,
            "validation": self.validation,
            **changes,
        }
        return query(self.db, self.source, self.context, **arguments)

    def _shaped(self, rows: Sequence[Any]) -> Sequence[Any]:
        if self.type is None:
            return rows
        adapter = _adapter(self.type)
        return [
            adapter.validate_python(_as_python(row, self.type), **self.validation)
            for row in rows
        ]

    def _shaped_one(self, row: Any) -> Any:  # noqa: ANN401
        if self.type is None or row is None:
            return row
        return _adapter(self.type).validate_python(
            _as_python(row, self.type), **self.validation
        )

    def _executable(self, *, size: int | None = None) -> Executable:
        if size is None:
            return self.statement
        return self.statement.execution_options(yield_per=size)


def require_pydantic() -> None:
    """Raise unless pydantic is installed, which `typed()` validates rows with.

    Raises:
        MissingDependencyError: if it is not.

    """
    if TypeAdapter is None:
        package, needed_by = "pydantic", "`typed()`"
        raise MissingDependencyError(package, needed_by)


def templates_of(db: BaseDatabase[Any, Any]) -> Templates:
    """Return the templates of this database, made once and kept on it."""
    templates = db.templates
    if not isinstance(templates, Templates):
        templates = Templates() if templates is None else Templates(templates)
        db.templates = templates
    return templates


def _statement(
    sql: str,
    params: Mapping[str, Any],
    *,
    label: str | None,
) -> sa.TextClause:
    """Return the SQL as a statement, with every value bound to it.

    The template's name goes in as a comment, so a slow query log and `Recording`
    say which file the SQL came from.

    Raises:
        StrayParameterError: if the SQL holds something SQLAlchemy reads as a
            parameter that nothing binds, such as a colon inside a JSON literal.

    """
    if label is not None:
        # `*/` in a name would end the comment early and leak into the SQL.
        sql = f"/* {label.replace('*/', '* /')} */\n{sql}"
    clause, named = _text(sql)
    stray = named - set(params)
    if stray:
        raise StrayParameterError(sorted(stray), label)
    # The context holds more than the SQL reads: bind only what it names.
    return clause.bindparams(
        *(_bound(name, value) for name, value in params.items() if name in named)
    )


_POSIX_CLASS = re.compile(
    r"\[:(alnum|alpha|blank|cntrl|digit|graph|lower|print|punct|space|upper|word|xdigit):\]"
)
"""A class in a regular expression, `[:punct:]`, which `text()` reads as `:punct`."""

_CAST_AFTER = re.compile(r"(?<![:\w\\])(:[A-Za-z_]\w*)(?=::)")
"""A parameter a cast follows, `:id::uuid`, which `text()` does not read as one."""


_CACHED_TEXT = 8192
"""The longest SQL `_text` keeps. `tpl.values` writes a parameter per value, so
each row count is new SQL, and a clause holds a bind parameter for each."""


def _text(sql: str) -> tuple[sa.TextClause, frozenset[str]]:
    """Return the SQL as `text()`, and the names of the parameters it holds.

    Reading the parameters out of the SQL is most of what building a statement
    costs, and a template renders the same SQL whenever its values have the same
    shape. Sharing the clause is safe: `bindparams` returns a copy. SQL longer
    than `_CACHED_TEXT` is read each time, which costs little next to running it.
    """
    return _parsed(sql) if len(sql) <= _CACHED_TEXT else _parsed.__wrapped__(sql)


@lru_cache(maxsize=1024)
def _parsed(sql: str) -> tuple[sa.TextClause, frozenset[str]]:
    """Read the SQL into `text()` and the names of its parameters: see `_text`."""
    sql = _CAST_AFTER.sub(r"\1 ", _POSIX_CLASS.sub(r"[\\:\1\\:]", sql))
    clause = sa.text(sql)
    named = frozenset(
        element.key
        for element in clause.get_children()
        if isinstance(element, sa.BindParameter)
    )
    return clause, named


def _bound(name: str, value: Any) -> sa.BindParameter[Any]:  # noqa: ANN401
    """Return the parameter to bind, as the value it holds asks to be bound.

    A sequence becomes an expanding parameter, so `IN :ids` is a list rather
    than a syntax error. A `bindparam` of your own carries its type through,
    which is how a value the driver cannot type is spelled out.
    """
    if isinstance(value, sa.BindParameter):
        return sa.bindparam(
            name,
            _expandable(value.value),
            type_=value.type,
            expanding=value.expanding or _expands(value.value),
        )
    return sa.bindparam(name, _expandable(value), expanding=_expands(value))


def _expands(value: Any) -> bool:  # noqa: ANN401
    """Whether this is many values rather than one."""
    return isinstance(value, list | tuple | set | frozenset)


def _expandable(value: Any) -> Any:  # noqa: ANN401
    """Return it as the list an expanding parameter takes."""
    return list(value) if _expands(value) else value


def _as_python(row: Any, type_: Any) -> Any:  # noqa: ANN401
    """Return what pydantic validates: the whole row, or one column of it.

    The type decides. One that reads a mapping takes the row as a mapping;
    anything else takes the first column's value, so `SELECT count(*)` reads
    as an `int` and a JSON column reads as what it holds.
    """
    mapping = getattr(row, "_mapping", None)
    if mapping is None:
        return row
    if _reads_a_row(type_):
        return dict(mapping)
    return next(iter(mapping.values()), None)


@cache
def _reads_a_row(type_: Any) -> bool:  # noqa: ANN401
    """Whether this type is built from a row's columns rather than one value."""
    origin = get_origin(type_) or type_
    if is_typeddict(type_):
        return True
    if not isinstance(origin, type):
        return False
    if BaseModel is not None and issubclass(origin, BaseModel):
        return True
    if is_dataclass(origin):
        return True
    if issubclass(origin, tuple) and hasattr(origin, "_fields"):  # a NamedTuple
        return True
    return issubclass(origin, Mapping)


@cache
def _adapter(type_: Any) -> TypeAdapter[Any]:  # noqa: ANN401
    """Return the adapter for this type, built once for the process."""
    return TypeAdapter(type_)
