"""Every built-in macro, run on every database the suite reaches.

The unit tests read the SQL each macro writes. These run it, so a form a
database refuses, or reads differently, shows up here: case, collation, `NULL`
ordering, `VALUES`, JSON and arrays.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from sqlakit import Database, MacroArgumentError
from sqlakit._sql import registered
from sqlakit.sql import Templates

ROWS = [(1, "Alpha", 10), (2, "beta", 20), (3, "Gamma", None)]
"""`id`, `name` and `score` of the rows every case reads."""

ONLY_POSTGRES = {"mysql", "mariadb", "oracle"}
"""The databases in the suite with no arrays."""


@dataclass(frozen=True)
class Case:
    """A template, the values it is called with, and the rows it returns."""

    macro: str
    sql: str
    values: dict[str, Any] = field(default_factory=dict)
    rows: Any = None
    by_dialect: dict[str, Any] = field(default_factory=dict)
    """Rows a database returns in its own way, or the error it gets."""
    refused: frozenset[str] = frozenset()
    """The databases the macro has no form for."""


CASES = [
    Case(
        "if_set",
        "SELECT id FROM macro_items WHERE tpl.if_set(:n, name = :n) ORDER BY id",
        {"n": "beta"},
        [2],
    ),
    Case(
        "if_set",
        "SELECT id FROM macro_items WHERE tpl.if_set(:n, name = :n) ORDER BY id",
        {"n": None},
        [1, 2, 3],
    ),
    Case(
        "unless_set",
        "SELECT id FROM macro_items WHERE tpl.unless_set(:every, score IS NOT NULL) ORDER BY id",
        {"every": None},
        [1, 2],
    ),
    Case(
        "unless_set",
        "SELECT id FROM macro_items WHERE tpl.unless_set(:every, score IS NOT NULL) ORDER BY id",
        {"every": True},
        [1, 2, 3],
    ),
    Case(
        "if_not_set",
        "SELECT id FROM macro_items WHERE tpl.if_not_set(:every, score IS NOT NULL) ORDER BY id",
        {"every": None},
        [1, 2],
    ),
    Case(
        "order_by",
        "SELECT id FROM macro_items ORDER BY tpl.order_by(:sort, id, score)",
        {"sort": "score.desc.nulls_last"},
        [2, 1, 3],
    ),
    # Nothing to sort by writes a term each database takes: `id` sorts after it.
    Case(
        "order_by",
        "SELECT id FROM macro_items ORDER BY tpl.order_by(:sort, score), id",
        {"sort": None},
        [1, 2, 3],
    ),
    Case(
        "order_by",
        "SELECT id FROM macro_items ORDER BY tpl.order_by(:sort, id, score)",
        {"sort": "score.asc.nulls_first"},
        [3, 1, 2],
    ),
    Case(
        "icollate",
        "SELECT id FROM macro_items ORDER BY tpl.order_by(:sort, id, name = tpl.icollate(name))",
        {"sort": "name"},
        [1, 2, 3],
    ),
    Case(
        "icollate",
        "SELECT id FROM macro_items WHERE tpl.icollate(name) = tpl.icollate(:n)",
        {"n": "ALPHA"},
        [1],
    ),
    Case(
        "icontains",
        "SELECT id FROM macro_items WHERE tpl.icontains(name, :q) ORDER BY id",
        {"q": "AMM"},
        [3],
    ),
    Case(
        "icontains",
        "SELECT id FROM macro_items WHERE tpl.icontains(name, :q) ORDER BY id",
        {"q": "a%"},
        [],
    ),
    Case(
        "between",
        "SELECT id FROM macro_items WHERE tpl.between(score, :low, :high, '[)') ORDER BY id",
        {"low": 10, "high": 20},
        [1],
    ),
    Case(
        "between",
        "SELECT id FROM macro_items WHERE tpl.between(score, :low, :high) ORDER BY id",
        {"low": None, "high": 15},
        [1],
    ),
    Case(
        "identifier",
        "SELECT id FROM macro_items ORDER BY tpl.identifier(:column, id, score) DESC",
        {"column": "id"},
        [3, 2, 1],
    ),
    Case(
        "each",
        "SELECT id FROM macro_items WHERE id IN (tpl.each(:ids)) ORDER BY id",
        {"ids": [1, 3]},
        [1, 3],
    ),
    Case(
        "in_list",
        "SELECT id FROM macro_items WHERE tpl.in_list(id, :ids, :exclude) ORDER BY id",
        {"ids": [1, 3], "exclude": True},
        [2],
    ),
    Case(
        "in_list",
        "SELECT id FROM macro_items WHERE tpl.in_list(id, :ids, :exclude) ORDER BY id",
        {"ids": [], "exclude": False},
        [1, 2, 3],
    ),
    Case(
        "values",
        "SELECT column2 FROM tpl.values(:rows) v ORDER BY column1",
        {"rows": [(2, "b"), (1, "a")]},
        ["a", "b"],
    ),
    Case(
        "json_object",
        "SELECT tpl.json_object('id', id, 'name', name) FROM macro_items WHERE id = 1",
        {},
        [{"id": 1, "name": "Alpha"}],
    ),
    Case(
        "string_agg",
        "SELECT tpl.string_agg(name, ',', id) FROM macro_items",
        {},
        ["Alpha,beta,Gamma"],
    ),
    Case(
        "array_agg",
        "SELECT tpl.array_agg(id, id) FROM macro_items",
        {},
        [[1, 2, 3]],
        refused=frozenset(ONLY_POSTGRES),
    ),
    Case(
        "array",
        "SELECT id FROM macro_items WHERE id = ANY(tpl.array(:ids, 'integer')) ORDER BY id",
        {"ids": [1, 2]},
        [1, 2],
        refused=frozenset(ONLY_POSTGRES),
    ),
    Case(
        "array_contains",
        "SELECT id FROM macro_items WHERE tpl.array_contains(tpl.array(:ids, 'integer'), id) ORDER BY id",
        {"ids": [2]},
        [2],
        refused=frozenset(ONLY_POSTGRES),
    ),
    Case(
        "arrays_overlap",
        "SELECT id FROM macro_items WHERE tpl.arrays_overlap(ARRAY[id], tpl.array(:ids, 'integer')) ORDER BY id",
        {"ids": [3]},
        [3],
        refused=frozenset(ONLY_POSTGRES),
    ),
    Case(
        "array_contains_all",
        "SELECT id FROM macro_items WHERE tpl.array_contains_all(ARRAY[id, 1], tpl.array(:ids, 'integer')) ORDER BY id",
        {"ids": [1]},
        [1, 2, 3],
        refused=frozenset(ONLY_POSTGRES),
    ),
    Case(
        "on_dialect",
        "SELECT tpl.on_dialect(postgresql = 'pg', mysql = 'my', oracle = 'ora') FROM macro_items WHERE id = 1",
        {},
        by_dialect={
            "postgres": ["pg"],
            "mysql": ["my"],
            "mariadb": ["my"],
            "oracle": ["ora"],
        },
    ),
    Case(
        "include", "SELECT count(*) FROM tpl.include('inner.sql') i", {"n": None}, [3]
    ),
]


@pytest.fixture
def items(db: Database, tmp_path: Path) -> Iterator[Database]:
    """The three rows, in a table of their own, and a template to include."""
    metadata = sa.MetaData()
    table = sa.Table(
        "macro_items",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(20)),
        sa.Column("score", sa.Integer, nullable=True),
    )
    with db.transaction() as conn:
        metadata.drop_all(conn)
        metadata.create_all(conn)
        conn.execute(
            table.insert(),
            [dict(zip(("id", "name", "score"), row, strict=True)) for row in ROWS],
        )
    (tmp_path / "inner.sql").write_text(
        "SELECT id FROM macro_items WHERE tpl.if_set(:n, name = :n)"
    )
    db.templates = Templates(tmp_path)
    yield db
    with db.transaction() as conn:
        metadata.drop_all(conn)


def _plain(value: Any) -> Any:
    """Read a value the way every database would agree on: JSON as data."""
    if isinstance(value, str) and value.startswith("{"):
        return json.loads(value)
    return value


@pytest.mark.parametrize(
    "case", CASES, ids=lambda case: f"{case.macro}-{sorted(case.values.items())}"
)
def test_a_built_in_macro_runs_on_this_database(
    items: Database, dialect: str, case: Case
) -> None:
    query = items.sql.from_string(case.sql, **case.values)
    if dialect in case.refused:
        with pytest.raises(MacroArgumentError, match="has no form for"):
            query.statement
        return
    with items.connect():
        rows = [_plain(value) for value in query.scalars().all()]

    assert rows == case.by_dialect.get(dialect, case.rows)


def test_every_built_in_macro_has_a_case() -> None:
    assert {case.macro for case in CASES} == {*registered([]), "include"}
