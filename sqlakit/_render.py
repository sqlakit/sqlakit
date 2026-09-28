"""Every template of a project, rendered, for a linter to read.

A macro written in Python is SQL only once it runs, so a linter reading the
templates reads its call and never what it writes. `rendered` runs the macros:
each template is written twice, once with every parameter given a made-up
value and once with none, as a macro decides its SQL by what it is given.
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import sqlalchemy.engine.default
import sqlalchemy.exc

from ._linters import parse_errors
from ._project import Problem
from ._sql import _rendered, calls_kept, registered
from ._static import StaticMacro

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from ._linters import Linter
    from ._project import Project

__all__ = ["Rendered", "import_macros", "rendered", "unparsed"]

VARIANTS = ("given", "not_given")
"""The two ways each template is written: every parameter given, and none."""


class Example:
    """A made-up value: any path read off it is another, and it writes `x`.

    `:criteria.teams` and `:filters["kind"]` both read one, so a template
    renders whatever shape its values have.
    """

    def __getattr__(self, _: str) -> Example:
        return self

    def __getitem__(self, _: object) -> Example:
        return self

    def __iter__(self) -> Iterator[Example]:
        return iter((self,))

    def __len__(self) -> int:
        return 1

    def __str__(self) -> str:
        return "x"


@dataclass(frozen=True, slots=True)
class Piece:
    """The stretch of rendered SQL a macro call wrote, and where the call is."""

    start: int
    end: int
    call: str
    """The call's name under its namespace: `tpl.owned_by`."""
    span: tuple[int, int]
    """Where the call is written in the template."""


@dataclass(frozen=True, slots=True)
class Rendered:
    """A template written one way, or why it could not be."""

    name: str
    variant: str
    sql: str | None
    problem: str | None = None
    kept: tuple[str, ...] = ()
    """The macro calls that could not be made, and stay calls in the SQL."""
    pieces: tuple[Piece, ...] = ()
    """What each call of the template itself wrote, in order."""
    read: bool = True
    """Whether the template could be read: `sqlakit check` says why not."""

    def written_by(self, offset: int) -> Piece | None:
        """Return the call that wrote the SQL at an offset, if a call did."""
        return next(
            (piece for piece in self.pieces if piece.start <= offset < piece.end), None
        )


def import_macros(project: Project) -> None:
    """Import the modules of the project's Python macros, in place of their reading.

    `load_project` reads the macros from their source, so a call to one is
    checked and never made. The modules are imported the way the code names
    them, from the directory above their package.

    Raises:
        Exception: whatever importing the project's code raises.

    """
    macros = project.templates.macros
    modules = {
        macro.path for macro in macros.values() if isinstance(macro, StaticMacro)
    }
    for path in sorted(modules):
        folder, name = _module(path)
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
        found = registered([name])
        macros.update(
            (key, macro)
            for key, macro in found.items()
            if isinstance(macros.get(key), StaticMacro)
        )


def rendered(project: Project, dialect: str) -> Iterator[Rendered]:
    """Yield each template of the project, written each way."""
    preparer = _preparer(dialect)
    namespace = project.templates.namespace
    for name in project.templates.names():
        path = project.path_of(name)
        if path is None:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            template = project.load(name, source)
        except Exception as error:  # noqa: BLE001 - `sqlakit check` says more
            for variant in VARIANTS:
                yield Rendered(name, variant, None, str(error), read=False)
            continue
        calls = list(_calls(template.parts))
        refused = _refused(project, name, source, calls, preparer)
        for variant in VARIANTS:
            given = variant == "given"
            values: dict[str, Any] = {
                param: Example() if given and param not in refused else None
                for param in template.parameters()
            }
            try:
                with calls_kept():
                    sql, _ = _rendered(
                        template, {"dialect": dialect, **values}, preparer
                    )
            except Exception as error:  # noqa: BLE001 - the project's macros raise anything
                yield Rendered(name, variant, None, f"{type(error).__name__}: {error}")
                continue
            kept = tuple(
                sorted(
                    {
                        f"{namespace}.{call.name}"
                        for call in calls
                        if re.search(
                            rf"(?<![\w.]){re.escape(namespace)}\.{re.escape(call.name)}\s*\(",
                            sql,
                            re.IGNORECASE,
                        )
                    }
                )
            )
            sql = sql.strip() + "\n"
            pieces = _pieces(
                project,
                name,
                source,
                parts=template.parts,
                values=values,
                preparer=preparer,
                sql=sql,
            )
            yield Rendered(name, variant, sql, kept=kept, pieces=pieces)


def _pieces(  # noqa: PLR0913 - the template, and what it was rendered with
    project: Project,
    name: str,
    source: str,
    *,
    parts: Sequence[Any],
    values: dict[str, Any],
    preparer: Any,  # noqa: ANN401
    sql: str,
) -> tuple[Piece, ...]:
    """Return what each call of the template wrote, found in the whole SQL.

    Each call is rendered alone with the same values, and looked for after the
    one before it.
    """
    namespace = project.templates.namespace
    dialect = getattr(preparer.dialect, "name", "")
    found = []
    after = 0
    for call in parts:
        if isinstance(call, str):
            continue
        try:
            alone = project.load(name, source[call.span[0] : call.span[1]])
            with calls_kept():
                text, _ = _rendered(
                    alone,
                    {
                        "dialect": dialect,
                        **{param: values.get(param) for param in alone.parameters()},
                    },
                    preparer,
                )
        except Exception:  # noqa: BLE001, S112 - a call that fails alone has no piece
            continue
        text = text.strip()
        start = sql.find(text, after) if text else -1
        if start < 0:
            continue
        after = start + len(text)
        found.append(Piece(start, after, f"{namespace}.{call.name}", call.span))
    return tuple(found)


_WAYS = {"given": "with every parameter given", "not_given": "with none given"}


def unparsed(project: Project, linter: Linter, dialect: str) -> list[Problem]:
    """Return where the linter cannot parse what the templates render.

    Each template is rendered each way, and the SQL the linter cannot parse is
    put on the call that wrote it. SQL the template writes itself is put on
    its line, found by its text. A template that cannot be rendered is a
    problem too.
    """
    written = list(rendered(project, dialect))
    with tempfile.TemporaryDirectory() as folder:
        files = {}
        for index, one in enumerate(written):
            if one.sql is not None:
                file = Path(folder, f"{index}.sql").resolve()
                file.write_text(one.sql, encoding="utf-8")
                files[file] = one
        errors = parse_errors(linter, Path(folder).resolve(), project.root)
    found: dict[tuple[Path, int, int, str], list[str]] = {}
    for one in written:
        path = project.path_of(one.name)
        if path is None or one.sql is not None or not one.read:
            continue
        key = (path, 0, 0, f"cannot be rendered {{}}: {one.problem}")
        found.setdefault(key, []).append(_WAYS[one.variant])
    for file, places in errors.items():
        one = files.get(file)
        path = project.path_of(one.name) if one else None
        if one is None or one.sql is None or path is None:
            continue
        source = path.read_text(encoding="utf-8")
        starts = [0, *(index + 1 for index, char in enumerate(one.sql) if char == "\n")]
        for line, column in places:
            offset = starts[min(line, len(starts)) - 1] + column - 1
            text = one.sql[starts[line - 1] :].split("\n", 1)[0].strip()
            piece = one.written_by(offset)
            if piece is not None:
                what = " ".join(one.sql[piece.start : piece.end].split())
                message = (
                    f"{piece.call} writes SQL {linter.name} cannot parse, {{}}: {what}"
                )
                key = (path, *piece.span, message)
            else:
                at = source.find(text) if text else -1
                start = max(at, 0)
                key = (
                    path,
                    start,
                    start + len(text),
                    f"{linter.name} cannot parse the rendered SQL, {{}}: {text}",
                )
            ways = found.setdefault(key, [])
            if _WAYS[one.variant] not in ways:
                ways.append(_WAYS[one.variant])
    return [
        Problem(path, start, end, message.format(" and ".join(ways)))
        for (path, start, end, message), ways in found.items()
    ]


def _refused(
    project: Project,
    name: str,
    source: str,
    calls: Sequence[Any],
    preparer: Any,  # noqa: ANN401
) -> set[str]:
    """Return the parameters a macro refuses made up, and takes when not given.

    `tpl.order_by` takes a sort that names one of its columns, and nothing
    made up does, so `:sort` counts as not given when the rest are.
    """
    found: set[str] = set()
    namespace = project.templates.namespace
    dialect = getattr(preparer.dialect, "name", "")
    for call in calls:
        text = source[call.span[0] : call.span[1]]
        try:
            alone = project.load(name, text)
        except Exception:  # noqa: BLE001, S112 - the whole template says what is wrong
            continue
        names = alone.parameters()
        if not names:
            continue
        written = []
        for value in (Example(), None):
            try:
                with calls_kept():
                    sql, _ = _rendered(
                        alone,
                        {"dialect": dialect, **dict.fromkeys(names, value)},
                        preparer,
                    )
            except Exception:  # noqa: BLE001 - a macro may refuse either
                sql = None
            written.append(sql)
        made_up, missing = (
            sql is None
            or bool(
                re.match(
                    rf"{re.escape(namespace)}\.{re.escape(call.name)}\s*\(",
                    sql.strip(),
                    re.IGNORECASE,
                )
            )
            for sql in written
        )
        if made_up and not missing:
            found.update(names)
    return found


def _calls(parts: Sequence[Any]) -> Iterator[Any]:
    """Yield every macro call of a read template, the ones in arguments too."""
    for part in parts:
        if isinstance(part, str):
            continue
        yield part
        for argument in part.args:
            yield from _calls(argument.parts)


def _module(path: Path) -> tuple[Path, str]:
    """Return the directory a module is imported from, and its dotted name."""
    parts = [path.stem]
    folder = path.parent
    while (folder / "__init__.py").is_file():
        parts.append(folder.name)
        folder = folder.parent
    return folder, ".".join(reversed(parts))


def _preparer(dialect: str) -> Any:  # noqa: ANN401 - SQLAlchemy's IdentifierPreparer
    """Return how a dialect quotes names, loaded without a driver or a server."""
    try:
        return sa.engine.make_url(f"{dialect}://").get_dialect()().identifier_preparer
    except sa.exc.NoSuchModuleError:
        default = sa.engine.default.DefaultDialect()
        default.name = dialect
        return default.identifier_preparer
