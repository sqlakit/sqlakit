"""`sqlakit`, the command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ._linters import LINTERS, settings, stale
from ._project import Problem, Project, load_project
from ._pycharm import DIALECTS, ddl, dialects
from ._sql import NAMESPACE, registered, signature_of
from .exceptions import MacroDefinitionError, ProjectConfigError, UnknownImportPathError


def main(argv: list[str] | None = None) -> int:
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

    export = commands.add_parser(
        "export", help="write what another tool needs to read the templates"
    )
    export.add_argument("tool", choices=(*LINTERS, "pycharm"))
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
        return _check(Path(arguments.project), json_output=arguments.format == "json")
    if arguments.command == "export":
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


def _check(directory: Path, *, json_output: bool) -> int:
    """Print what is wrong with the project's templates, and fail if anything is.

    The exit is 1 for problems in the templates, and 2 for a project that cannot
    be read, or holds no template to check.
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
