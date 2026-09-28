"""The `.sql-formatter.json` that lets `sql-formatter` format templates.

`sql-formatter` reads `:name` as a parameter only when `paramTypes` says so.
Without it, `c = :limit::int` on PostgreSQL comes back split over lines, with
the parameter gone. A macro call is a function call to it, and needs nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .exceptions import ProjectConfigError

if TYPE_CHECKING:
    from ._project import Project

__all__ = ["FILE", "config", "stale"]

FILE = ".sql-formatter.json"
"""Where `sql-formatter` looks for its settings, in the directory it runs in."""

LANGUAGES = {
    "bigquery",
    "clickhouse",
    "db2",
    "duckdb",
    "hive",
    "mariadb",
    "mysql",
    "postgresql",
    "redshift",
    "snowflake",
    "sqlite",
    "trino",
}
"""The dialects `sql-formatter` names as SQLAlchemy does."""

_RENAMED = {"oracle": "plsql", "mssql": "transactsql", "databricks": "spark"}
"""`sql-formatter`'s name for a dialect SQLAlchemy names another way."""


def stale(written: str | None) -> bool:
    """Whether the file is missing, or does not read `:name` as a parameter."""
    if written is None:
        return True
    try:
        settings = _read(written)
    except ProjectConfigError:
        return True
    named = settings.get("paramTypes", {}).get("named", [])
    return "language" not in settings or ":" not in named


def config(project: Project, written: str | None, dialect: str | None) -> str:
    """Return the file, with what `sql-formatter` needs to format the templates.

    `:` is added to the named parameters, and the language is written when the
    file has none. Every other setting stays as it is.

    Raises:
        ProjectConfigError: if the file is not a JSON object.

    """
    settings = _read(written) if written is not None else {}
    if "language" not in settings:
        chosen = (dialect or project.dialect or "").lower()
        settings["language"] = _RENAMED.get(chosen) or (
            chosen if chosen in LANGUAGES else "sql"
        )
    types = settings.setdefault("paramTypes", {})
    if not isinstance(types, dict):
        problem = f"`paramTypes` in {FILE} is not an object: make it one, or remove it"
        raise ProjectConfigError(problem)
    named = types.setdefault("named", [])
    if ":" not in named:
        named.append(":")
    return json.dumps(settings, indent=2) + "\n"


def _read(written: str) -> dict[str, Any]:
    """Return the settings a file holds."""
    try:
        settings = json.loads(written)
    except json.JSONDecodeError as error:
        problem = f"{FILE} is not JSON ({error}): fix it, or remove it"
        raise ProjectConfigError(problem) from error
    if not isinstance(settings, dict):
        problem = f"{FILE} is not a JSON object: fix it, or remove it"
        raise ProjectConfigError(problem)
    return settings
