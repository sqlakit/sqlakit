"""The settings in `pyproject.toml` that let `sqruff` or `sqlfluff` read templates.

The `placeholder` templater writes each `:name` as `name`. Where that reads as
something else, `settings` gives the parameter a value; see
`Project.placeholder_values`. Which names a dialect keeps for itself is asked
of the linter, when it is installed, since its dialects keep words SQLAlchemy's
do not: `exclude` and `row` on SQLite.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._project import LINT_EXCLUDED
from .exceptions import ProjectConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._project import Project

__all__ = ["LINTERS", "Linter", "dialect_of", "parse_errors", "settings", "stale"]

_NEXT_TABLE = re.compile(r"^\[", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Linter:
    """A linter that reads templates with its `placeholder` templater."""

    name: str
    """The executable, and the key of its tables under `[tool]`."""
    dialects: dict[str, str]
    """The linter's name for a dialect SQLAlchemy names another way."""
    rules: tuple[str, ...]
    """The lines of `[tool.<name>.core]` that choose the rules."""
    default_dialect: str | None
    """The dialect written when neither the call nor the project gives one."""
    kept: Callable[[str, list[str], str | None], set[int]]
    """Given the executable, the names and the dialect, the index of each name it
    cannot read as a column."""
    unparsed: Callable[[str, Path, Path], dict[Path, list[tuple[int, int]]]]
    """Given the executable, a directory of SQL and the project's root, where in
    each file it could not parse: the line and the column, from one."""

    @property
    def placeholder(self) -> str:
        return f"[tool.{self.name}.templater.placeholder]"

    @property
    def core(self) -> str:
        return f"[tool.{self.name}.core]"


def stale(linter: Linter, project: Project, pyproject: str) -> list[str]:
    """Return the tables of `pyproject.toml` that no longer read the templates."""
    tables = tomllib.loads(pyproject).get("tool", {}).get(linter.name, {})
    written = tables.get("templater", {}).get("placeholder", {})
    chosen = _dialect(linter, tables, None, project)
    values = project.placeholder_values(_kept(linter, project, chosen))
    checked = (
        (linter.core, "core" in tables),
        (
            linter.placeholder,
            all(str(written.get(name)) == str(value) for name, value in values.items()),
        ),
    )
    return [table for table, fresh in checked if not fresh]


def settings(
    linter: Linter, project: Project, pyproject: str, dialect: str | None
) -> str:
    """Return `pyproject.toml` with the settings that read the templates.

    The values the templates need are written again each time, and a value
    added to the table by hand stays. The core table is written only when it
    is missing, so the rules set there stay.

    Raises:
        ProjectConfigError: if the table is written in a way this cannot rewrite.

    """
    tables = tomllib.loads(pyproject).get("tool", {}).get(linter.name, {})
    written = dict(tables.get("templater", {}).get("placeholder", {}))
    written.pop("param_style", None)
    text = pyproject
    chosen = _dialect(linter, tables, dialect, project)
    if "core" not in tables:
        core = [
            linter.core,
            *([f'dialect = "{chosen}"'] if chosen else []),
            'templater = "placeholder"',
            *linter.rules,
            f'exclude_rules = "{",".join(LINT_EXCLUDED)}"',
        ]
        text = text.rstrip("\n") + "\n\n" + "\n".join(core) + "\n"
    values = project.placeholder_values(_kept(linter, project, chosen))
    merged = dict(sorted({**written, **values}.items()))
    table = "\n".join(
        [
            linter.placeholder,
            'param_style = "colon"',
            *(f"{name} = {_toml_value(value)}" for name, value in merged.items()),
        ]
    )
    header = re.compile(
        rf"^{re.escape(linter.placeholder)}[ \t]*(?:#[^\n]*)?$", re.MULTILINE
    )
    if found := header.search(text):
        following = _NEXT_TABLE.search(text, found.end())
        end = following.start() if following else len(text)
        after = "\n\n" if following else "\n"
        written_out = text[: found.start()] + table + after + text[end:]
    else:
        written_out = text.rstrip("\n") + "\n\n" + table + "\n"
    try:
        tomllib.loads(written_out)
    except tomllib.TOMLDecodeError as error:
        problem = (
            f"`{linter.placeholder}` is written in a way this "
            f"cannot rewrite ({error}): give it a table of its own"
        )
        raise ProjectConfigError(problem) from error
    return written_out


def dialect_of(linter: Linter, pyproject: str) -> str | None:
    """Return the dialect the linter's settings read in, by SQLAlchemy's name."""
    tables = tomllib.loads(pyproject).get("tool", {}).get(linter.name, {})
    written = tables.get("core", {}).get("dialect")
    if not isinstance(written, str):
        return None
    named = {theirs: ours for ours, theirs in linter.dialects.items()}
    return named.get(written, written)


def parse_errors(
    linter: Linter, folder: Path, root: Path
) -> dict[Path, list[tuple[int, int]]]:
    """Return where the linter could not parse each file of a directory.

    It runs in the project's root, so it reads the project's settings, and an
    empty result says the linter is not installed.
    """
    binary = shutil.which(linter.name)
    if binary is None:
        return {}
    return linter.unparsed(binary, folder, root)


def _sqruff_unparsed(
    binary: str, folder: Path, root: Path
) -> dict[Path, list[tuple[int, int]]]:
    found = subprocess.run(  # noqa: S603 - sqruff, found on the PATH
        [binary, "lint", "--parsing-errors", "--format", "json", str(folder)],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    return {
        Path(path).resolve(): [
            (problem["range"]["start"]["line"], problem["range"]["start"]["character"])
            for problem in problems
            # A problem of parsing has no rule, and so no code.
            if problem.get("code") is None
        ]
        for path, problems in _report(found.stdout, found.stderr, shape=dict).items()
    }


def _sqlfluff_unparsed(
    binary: str, folder: Path, root: Path
) -> dict[Path, list[tuple[int, int]]]:
    found = subprocess.run(  # noqa: S603 - sqlfluff, found on the PATH
        # A file outside the project reads no setting of it unless told where.
        [
            binary,
            "lint",
            "--config",
            str(root / "pyproject.toml"),
            "--format",
            "json",
            str(folder),
        ],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    return {
        Path(report["filepath"]).resolve(): [
            (problem["start_line_no"], problem["start_line_pos"])
            for problem in report.get("violations", ())
            if problem.get("code") == "PRS"
        ]
        for report in _report(found.stdout, found.stderr, shape=list)
    }


def _dialect(
    linter: Linter, tables: dict, given: str | None, project: Project
) -> str | None:
    """Return the dialect the linter reads the templates in, by its name."""
    if "core" in tables:
        # A table without a dialect is read as the linter reads it, and so here.
        written = tables["core"].get("dialect")
        return written if isinstance(written, str) else linter.default_dialect
    chosen = given or project.dialect
    if chosen is None:
        return linter.default_dialect
    return linter.dialects.get(chosen, chosen)


def _kept(linter: Linter, project: Project, dialect: str | None) -> set[str]:
    """Return the parameter names the linter cannot read as a column in the dialect.

    Without the linter installed, the dialects of SQLAlchemy decide alone.
    """
    binary = shutil.which(linter.name)
    names = sorted(project.parameter_names())
    if binary is None or not names:
        return set()
    return {names[index] for index in linter.kept(binary, names, dialect)}


def _sqruff_kept(binary: str, names: list[str], dialect: str | None) -> set[int]:
    """Lint each name as `SELECT <name>;`, one to a line, in one run of `sqruff`."""
    # JSON, as the report of its own is another shape on GitHub Actions.
    command = [binary, "lint", "--parsing-errors", "--format", "json", "-"]
    if dialect:
        command[2:2] = ["--dialect", dialect]
    source = "".join(f"SELECT {name};\n" for name in names)
    # Away from the project, so no setting of its own changes the reading.
    with tempfile.TemporaryDirectory() as empty:
        found = subprocess.run(  # noqa: S603 - sqruff, found on the PATH
            command,
            input=source,
            capture_output=True,
            text=True,
            cwd=empty,
            check=False,
        )
    lines = {
        problem["range"]["start"]["line"]
        for problems in _report(found.stdout, found.stderr, shape=dict).values()
        for problem in problems
        # The name starts after `SELECT `, at the eighth character.
        if problem.get("message") == "Unparsable section"
        and problem["range"]["start"]["character"] == len("SELECT ") + 1
    }
    return {index for index in range(len(names)) if index + 1 in lines}


def _sqlfluff_kept(binary: str, names: list[str], dialect: str | None) -> set[int]:
    """Lint each name as `SELECT <name>;`, a file each, in one run of `sqlfluff`.

    A file each, as `sqlfluff` reads the lines after one it cannot parse as
    part of it.
    """
    with tempfile.TemporaryDirectory() as empty:
        folder = Path(empty)
        for index, name in enumerate(names):
            (folder / f"{index}.sql").write_text(f"SELECT {name};\n", encoding="utf-8")
        command = [binary, "lint", "--format", "json", "--dialect", dialect or "ansi"]
        found = subprocess.run(  # noqa: S603 - sqlfluff, found on the PATH
            [*command, "."],
            capture_output=True,
            text=True,
            cwd=empty,
            check=False,
        )
    return {
        int(Path(report["filepath"]).stem)
        for report in _report(found.stdout, found.stderr, shape=list)
        for problem in report.get("violations", ())
        # The name starts after `SELECT `, at the eighth character.
        if problem.get("code") == "PRS"
        and problem.get("start_line_no") == 1
        and problem.get("start_line_pos") == len("SELECT ") + 1
    }


def _report(*outputs: str, shape: type) -> Any:  # noqa: ANN401 - either linter's JSON
    """Return the JSON report, from whichever stream the linter wrote it to."""
    for output in outputs:
        try:
            report = json.loads(output)
        except json.JSONDecodeError:
            continue
        if isinstance(report, shape):
            return report
    return shape()


def _toml_value(value: object) -> str:
    """Return a value as TOML writes it: a string quoted, a flag in lower case."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))


SQRUFF = Linter(
    name="sqruff",
    dialects={"postgresql": "postgres", "mariadb": "mysql", "mssql": "tsql"},
    # sqruff turns on a few rules unless told, and every one on `all`.
    rules=('rules = "all"',),
    # sqruff reads settings without a dialect as ANSI.
    default_dialect=None,
    kept=_sqruff_kept,
    unparsed=_sqruff_unparsed,
)

SQLFLUFF = Linter(
    name="sqlfluff",
    dialects={"postgresql": "postgres", "mssql": "tsql"},
    # sqlfluff turns on every rule unless told.
    rules=(),
    # sqlfluff reads no template without a dialect.
    default_dialect="ansi",
    kept=_sqlfluff_kept,
    unparsed=_sqlfluff_unparsed,
)

LINTERS = {linter.name: linter for linter in (SQRUFF, SQLFLUFF)}
"""Each linter `sqlakit export` writes settings for, by its name."""
