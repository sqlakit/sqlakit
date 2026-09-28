"""`sqlakit`, the command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import _sql_formatter
from ._linters import LINTERS, dialect_of, settings, stale
from ._project import Problem, Project, load_project
from ._pycharm import DIALECTS, ddl, dialects
from ._render import import_macros, rendered, unparsed
from ._sql import NAMESPACE, registered, signature_of
from .exceptions import MacroDefinitionError, ProjectConfigError, UnknownImportPathError


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - one return a command
    """Run a command, and return what the shell should make of it."""
    parser = argparse.ArgumentParser(prog="sqlakit", description="SQLAKit.")
    commands = parser.add_subparsers(dest="command", required=True)

    macros = commands.add_parser(
        "macros", help="list the macros the project's templates can call"
    )
    macros.add_argument(
        "modules",
        nargs="*",
        metavar="MODULE",
        help="list the built-in macros and this module's, or `module:name` for one",
    )
    macros.add_argument(
        "--markdown", action="store_true", help="write Markdown, for documentation"
    )
    macros.add_argument(
        "--namespace",
        help="the schema name calls are written under, if not the project's",
    )
    macros.add_argument(
        "--project",
        default=".",
        help="a directory in the project, the current one by default",
    )

    check = commands.add_parser(
        "check", help="check every template the project's code builds `Templates` with"
    )
    check.add_argument(
        "--project",
        default=".",
        help="a directory in the project, the current one by default",
    )
    check.add_argument("--format", choices=("text", "json"), default="text")
    check.add_argument(
        "--lint",
        choices=tuple(LINTERS),
        help="also render every template, running the project's macros, and "
        "fail where the linter cannot parse the SQL",
    )
    check.add_argument(
        "--dialect", help="the dialect to render for, in place of the project's"
    )

    render = commands.add_parser(
        "render",
        help="write every template as the SQL it renders, for a linter to read",
    )
    render.add_argument(
        "--out", required=True, help="the directory to write the rendered SQL into"
    )
    render.add_argument(
        "--project",
        default=".",
        help="a directory in the project, the current one by default",
    )
    render.add_argument(
        "--dialect", help="the dialect to render for, in place of the project's"
    )

    export = commands.add_parser(
        "export", help="write what another tool needs to read the templates"
    )
    export.add_argument("tool", choices=(*LINTERS, "sql-formatter", "pycharm"))
    export.add_argument(
        "--project",
        default=".",
        help="a directory in the project, the current one by default",
    )
    export.add_argument(
        "--dialect", help="the dialect to write for, in place of the project's"
    )
    export.add_argument(
        "--check",
        action="store_true",
        help="write nothing, and fail if what is written is out of date",
    )

    arguments = parser.parse_args(argv)
    if arguments.command == "check":
        return _check(
            Path(arguments.project),
            json_output=arguments.format == "json",
            lint=arguments.lint,
            dialect=arguments.dialect,
        )
    if arguments.command == "render":
        return _render(
            Path(arguments.project), Path(arguments.out), dialect=arguments.dialect
        )
    if arguments.command == "export":
        if arguments.tool == "sql-formatter":
            return _export_sql_formatter(
                Path(arguments.project),
                dialect=arguments.dialect,
                check=arguments.check,
            )
        if arguments.tool == "pycharm":
            return _export_pycharm(
                Path(arguments.project),
                dialect=arguments.dialect,
                check=arguments.check,
            )
        return _export_linter(
            arguments.tool,
            Path(arguments.project),
            dialect=arguments.dialect,
            check=arguments.check,
        )
    if arguments.command == "macros":
        return _macros(
            arguments.modules,
            Path(arguments.project),
            markdown=arguments.markdown,
            namespace=arguments.namespace,
        )
    return 1


def _macros(
    modules: list[str], directory: Path, *, markdown: bool, namespace: str | None
) -> int:
    """Print every macro the project's templates can call, under its namespace.

    Given modules, print the built-in macros and theirs. Outside a project, the
    built-in ones under `tpl`.
    """
    project = None
    if not modules or namespace is None:
        try:
            project = load_project(directory)
        except ProjectConfigError:
            project = None
    # A module of the project imports from its root, installed or not.
    sys.path.insert(0, str(project.root if project else directory.resolve()))
    try:
        found = registered(modules) if modules or project is None else None
    except (UnknownImportPathError, MacroDefinitionError) as error:
        _say(str(error))
        return 2
    if found is None:
        assert project is not None  # noqa: S101 - read above when no module is named
        found = project.templates.macros
    if namespace is None:
        namespace = NAMESPACE if project is None else project.templates.namespace
    for macro in found.values():
        signature = signature_of(macro, namespace)
        if markdown:
            _say(f"### `{signature}`\n\n{macro.doc}\n")
        else:
            summary = macro.doc.split("\n", 1)[0]
            _say(f"{_paint(signature, BOLD)}\n    {summary}")
    return 0


def _load(directory: Path, *, json_output: bool = False) -> Project | None:
    """Read the project, or say why it cannot be read and return None."""
    try:
        return load_project(directory)
    except ProjectConfigError as error:
        _say(json.dumps({"error": str(error)}) if json_output else str(error))
        return None


def _check(
    directory: Path, *, json_output: bool, lint: str | None, dialect: str | None
) -> int:
    """Print what is wrong with the project's templates, and fail if anything is.

    With a linter, every template is rendered too, and what it cannot parse is
    a problem. The exit is 1 for problems in the templates, and 2 for a project
    that cannot be read, holds no template to check, or cannot be linted.
    """
    project = _load(directory, json_output=json_output)
    if project is None:
        return 2
    if not project.templates.names():
        where = ", ".join(str(path) for path in project.templates.paths)
        message = f"no template to check under {where}"
        _say(json.dumps({"error": message}) if json_output else message)
        return 2
    problems = list(project.problems())
    if lint is not None:
        try:
            problems.extend(_lint(project, lint, dialect))
        except _CannotRenderError as error:
            _say(json.dumps({"error": str(error)}) if json_output else str(error))
            return 2
    if json_output:
        _say(json.dumps([_as_json(problem) for problem in problems], indent=2))
    else:
        _say(_paint("\n".join(project.found), DIM) + "\n")
        for problem in problems:
            line, column = problem.position()
            _say(f"{_relative(problem.path)}:{line}:{column}: {problem.message}")
        count = len(project.templates.names())
        _say(
            _paint(
                f"{_counted(count, 'template')}, {_counted(len(problems), 'problem')}",
                DIM,
            )
        )
    return 1 if problems else 0


_WRITTEN = {"given": "every parameter given", "not_given": "no parameter given"}


class _CannotRenderError(Exception):
    """Why the templates cannot be rendered, or the rendering linted."""


def _lint(project: Project, name: str, given: str | None) -> list[Problem]:
    """Return where the linter cannot parse what the templates render.

    Raises:
        _CannotRenderError: if the linter cannot read the templates as the
            project is set up.

    """
    linter = LINTERS[name]
    if shutil.which(name) is None:
        problem = f"`{name}` is not installed, so the rendered SQL cannot be linted"
        raise _CannotRenderError(problem)
    pyproject = project.root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    if stale(linter, project, text):
        problem = (
            f"the `{name}` settings do not read the templates: run "
            f"`sqlakit export {name}` first"
        )
        raise _CannotRenderError(problem)
    configured = dialect_of(linter, text)
    chosen = _dialect_to_render(project, given, configured)
    if configured is not None and configured != chosen:
        problem = (
            f"the templates render on {chosen}, and `{name}` reads {configured}: "
            f"pass --dialect {configured}, or set the dialect in `[tool.{name}.core]`"
        )
        raise _CannotRenderError(problem)
    try:
        import_macros(project)
    except Exception as error:
        problem = (
            f"the project's macros cannot be imported: {type(error).__name__}: {error}"
        )
        raise _CannotRenderError(problem) from error
    return unparsed(project, linter, chosen)


def _dialect_to_render(
    project: Project, given: str | None, configured: str | None = None
) -> str:
    """Return the dialect to render the templates on.

    `--dialect` wins, then the one the project's database URL names, then the
    one a linter's settings read in. A macro writes by `ctx.dialect`, so a
    guess would render SQL the database never gets.

    Raises:
        _CannotRenderError: if nothing names one.

    """
    chosen = given or project.dialect or configured
    if chosen is None:
        problem = (
            "the code names no database URL to take the dialect from: pass --dialect"
        )
        raise _CannotRenderError(problem)
    return chosen.lower()


def _render(directory: Path, out: Path, *, dialect: str | None) -> int:
    """Write each template twice, rendered with its parameters given and not.

    The project's Python macros are imported and run, so what they write is
    in the SQL. Exit 1 when a template cannot be rendered.
    """
    project = _load(directory)
    if project is None:
        return 2
    try:
        import_macros(project)
    except Exception as error:  # noqa: BLE001 - the project's code raises anything
        _say(
            f"the project's macros cannot be imported: {type(error).__name__}: {error}"
        )
        return 2
    try:
        chosen = _dialect_to_render(project, dialect)
    except _CannotRenderError as error:
        _say(str(error))
        return 2
    problems = 0
    count = 0
    for one in rendered(project, chosen):
        if one.sql is None:
            problems += 1
            _say(f"{one.name} ({one.variant}): {one.problem}")
            continue
        target = out / f"{one.name.removesuffix('.sql')}.{one.variant}.sql"
        target.parent.mkdir(parents=True, exist_ok=True)
        header = [f"-- {one.name} on {chosen}, {_WRITTEN[one.variant]}"]
        if one.kept:
            header.append(
                f"-- {', '.join(one.kept)} could not be made, and stays a call"
            )
        target.write_text("\n".join(header) + "\n" + one.sql, encoding="utf-8")
        count += 1
    _say(f"wrote {count} files to {_relative(out)}")
    return 1 if problems else 0


def _export_linter(
    name: str, directory: Path, *, dialect: str | None, check: bool
) -> int:
    """Write the settings of `sqruff` or `sqlfluff` that read the templates."""
    linter = LINTERS[name]
    project = _load(directory)
    if project is None:
        return 2
    pyproject = project.root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    if check:
        tables = stale(linter, project, text)
        if tables:
            _say(f"{', '.join(tables)} out of date: run `sqlakit export {name}`")
            return 1
        return 0
    try:
        written = settings(linter, project, text, dialect)
    except ProjectConfigError as error:
        _say(str(error))
        return 2
    pyproject.write_text(written, encoding="utf-8")
    _say(f"wrote {_relative(pyproject)}")
    return 0


def _export_sql_formatter(directory: Path, *, dialect: str | None, check: bool) -> int:
    """Write the `.sql-formatter.json` that reads `:name` as a parameter."""
    project = _load(directory)
    if project is None:
        return 2
    path = project.root / _sql_formatter.FILE
    written = path.read_text(encoding="utf-8") if path.exists() else None
    if check:
        if _sql_formatter.stale(written):
            _say(f"{_relative(path)} out of date: run `sqlakit export sql-formatter`")
            return 1
        return 0
    try:
        text = _sql_formatter.config(project, written, dialect)
    except ProjectConfigError as error:
        _say(str(error))
        return 2
    path.write_text(text, encoding="utf-8")
    _say(f"wrote {_relative(path)}")
    return 0


def _export_pycharm(directory: Path, *, dialect: str | None, check: bool) -> int:
    """Write what PyCharm needs into `.idea/`: the macros as DDL, and the dialect."""
    project = _load(directory)
    if project is None:
        return 2
    chosen = (dialect or project.dialect or "postgresql").lower()
    if chosen not in DIALECTS:
        _say(
            f"PyCharm has no dialect `{chosen}`: pass one of {', '.join(sorted(DIALECTS))}"
        )
        return 2
    idea = project.root / ".idea"
    ddl_path = idea / "sqlakit.sql"
    mappings_path = idea / "sqldialects.xml"
    written = (
        mappings_path.read_text(encoding="utf-8") if mappings_path.exists() else None
    )
    wanted = {
        ddl_path: ddl(project, "snowflake" if chosen == "snowflake" else "postgresql"),
        mappings_path: dialects(
            project.root,
            [Path(path) for path in project.templates.paths],
            chosen,
            written,
        ),
    }
    if check:
        outdated = [
            _relative(path)
            for path, text in wanted.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        if outdated:
            _say(f"{', '.join(outdated)} out of date: run `sqlakit export pycharm`")
            return 1
        return 0
    idea.mkdir(exist_ok=True)
    for path, text in wanted.items():
        path.write_text(text, encoding="utf-8")
    _say(
        f"wrote {_relative(ddl_path)} and {_relative(mappings_path)}\n\n"
        "Once, in PyCharm:\n"
        f"  Database > + > DDL Data Source, and add {_relative(ddl_path)} to it\n"
        "  Settings > Tools > Database > User Parameters: add the pattern\n"
        r"    :(\w+(?:\.\w+)*)  with SQL checked"
    )
    return 0


def _as_json(problem: Problem) -> dict[str, object]:
    line, column = problem.position()
    return {
        "path": _relative(problem.path),
        "line": line,
        "column": column,
        "message": problem.message,
    }


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


BOLD = "1"
DIM = "2"


def _colours() -> bool:
    """Whether to paint: a terminal that wants it, and was not told otherwise."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _paint(text: str, *codes: str) -> str:
    """Return the text in those colours, or as it is where colour is unwanted."""
    if not codes or not _colours():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def _counted(number: int, thing: str) -> str:
    """Return `1 template`, or `2 templates`."""
    return f"{number} {thing}{'' if number == 1 else 's'}"


def _say(text: str) -> None:
    """Print and flush, so a pipe gets each line at once."""
    print(text, flush=True)  # noqa: T201


if __name__ == "__main__":
    sys.exit(main())
