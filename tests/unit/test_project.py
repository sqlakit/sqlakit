"""`sqlakit check` and `sqlakit export`: a project's templates, read from its code."""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from sqlakit import MacroArgumentError, ProjectConfigError
from sqlakit._cli import main
from sqlakit._project import load_project
from sqlakit._sql import signature_of

PYPROJECT = """
[project]
name = "app"
"""

DB = """
import os
from pathlib import Path

from sqlakit import Database
from sqlakit.sql import Templates

HERE = Path(__file__).parent

db = Database(
    os.environ["DATABASE_URL"],
    templates=Templates(HERE / "sql", macros=[HERE / "_macros.sql"]),
)
"""

FOUND = [
    "templates: sql (db.py:12)",
    "namespace: tpl (the default)",
    "macros: 1 in Python, 1 file of SQL macros",
    "dialect: not in the code, so `sqlakit export` takes --dialect",
]
"""What `sqlakit check` says it read of the project below."""

MACROS = '''
from sqlakit.sql import Param, sql_macro


@sql_macro
def mine(teams: Param) -> str:
    """Rows of any of the teams."""
    return f"team IN {teams}"
'''

SQL_MACROS = """-- Rows of the team the call asks for.
SELECT t.team = :team AS for_team FROM t;

SELECT tpl.for_team(t) OR t.public AS visible FROM t;
"""

TEMPLATES = {
    "good.sql": "SELECT * FROM users\nWHERE tpl.mine(:teams)\n  AND tpl.if_set(:q, name = :q)",
    "inner.sql": "SELECT 1\nWHERE tpl.nope(:x)",
    "outer.sql": "SELECT *\nFROM tpl.include('inner.sql') AS i",
    "open.sql": "SELECT 1,\n  'never closed",
}


APP = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "shop/__init__.py": "",
    "shop/db.py": """
from pathlib import Path

from sqlakit import Database
from sqlakit.sql import Templates

BASE_DIR = Path(__file__).parent / "sql"

db = Database(
    "sqlite://",
    templates=Templates(
        BASE_DIR,
        macros=["shop.macros", BASE_DIR / "_macros.sql"],
        namespace="q",
    ),
)
raise RuntimeError("imported")
""",
    "shop/macros.py": '''
from typing import Literal

from sqlakit.sql import Context, Param, Sql, sql_macro


@sql_macro(optional=True)
def owned(ctx: Context, team: Param, *columns: Sql) -> str:
    """Rows of the team."""
    raise RuntimeError("called")


@sql_macro(name="sided")
def side(which: Literal["'left'", "'right'"] = "'left'") -> str:
    return which
''',
    "shop/sql/_macros.sql": "-- Rows of the team.\nSELECT t.team = :team AS for_team FROM t;\n",
    "shop/sql/users.sql": "SELECT * FROM users AS u WHERE q.owned(:team, u.a) AND q.for_team(u)\n",
    "tests/test_it.py": "from sqlakit.sql import Templates\nTemplates('elsewhere')\n",
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "db.py").write_text(DB)
    (tmp_path / "macros.py").write_text(MACROS)
    (tmp_path / "_macros.sql").write_text(SQL_MACROS)
    for name, source in TEMPLATES.items():
        path = tmp_path / "sql" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_project_is_read_from_the_templates_its_code_builds(project: Path) -> None:
    loaded = load_project(project / "sql")

    assert loaded.root == project
    assert loaded.templates.paths == (project / "sql",)
    assert "mine" in loaded.templates.macros
    assert list(loaded.found) == FOUND


def test_pyproject_overrides_what_the_code_says(project: Path) -> None:
    (project / "queries").mkdir()
    (project / "pyproject.toml").write_text(
        PYPROJECT
        + '[tool.sqlakit.templates]\npaths = ["queries"]\ndialect = "snowflake"\n'
    )
    loaded = load_project(project)

    assert loaded.templates.paths == (project / "queries",)
    assert loaded.dialect == "snowflake"
    assert loaded.found[0] == "templates: queries (pyproject.toml)"
    assert loaded.found[-1] == "dialect: snowflake (pyproject.toml)"


@pytest.mark.parametrize(
    ("db", "namespace", "origin"),
    [
        (
            'NAMESPACE = "t"\nTemplates("sql", namespace=NAMESPACE)\n',
            "t",
            "app/db.py:2",
        ),
        (
            'from .settings import NAMESPACE\nTemplates("sql", namespace=NAMESPACE)\n',
            "t",
            "app/db.py:2",
        ),
        (
            'from . import settings\nTemplates("sql", namespace=settings.NAMESPACE)\n',
            "t",
            "app/db.py:2",
        ),
        (
            'import os\nTemplates("sql", namespace=os.environ["NAMESPACE"])\n',
            "t",
            (
                "the templates' calls, as app/db.py:2 passes it in a way the "
                "reading cannot follow"
            ),
        ),
    ],
)
def test_the_namespace_is_followed_wherever_the_code_keeps_it(
    tmp_path: Path, db: str, namespace: str, origin: str
) -> None:
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "q.sql").write_text("SELECT 1 WHERE t.if_set(:a, TRUE)\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "settings.py").write_text('NAMESPACE = "t"\n')
    (tmp_path / "app" / "db.py").write_text(db)
    loaded = load_project(tmp_path)

    assert loaded.templates.namespace == namespace
    assert (
        loaded.found[1]
        == f"namespace: {namespace} ({origin.replace('db.py', 'app/db.py', 1) if origin.startswith('db.py') else origin})"
    )


def test_the_macros_command_lists_the_project_under_its_namespace(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["macros", "--project", str(app)]) == 0
    listed = [line for line in capsys.readouterr().out.splitlines() if line[:1] != " "]

    assert listed[0] == "q.if_set(:value, expr[, otherwise])"
    assert listed[-3:] == [
        "q.owned(:team, *columns)",
        "q.sided([which])",
        "q.for_team(t)",
    ]


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        ('"postgresql+psycopg://localhost/app"', "postgresql"),
        ('os.environ.get("DATABASE_URL", "snowflake://account/db")', "snowflake"),
        ("URL", "mysql"),
        ('os.environ["DATABASE_URL"]', None),
    ],
)
def test_the_dialect_is_read_from_a_url_the_code_writes_out(
    tmp_path: Path, url: str, dialect: str | None
) -> None:
    (tmp_path / "sql").mkdir()
    (tmp_path / "db.py").write_text(
        f'import os\n\nURL = "mysql+pymysql://localhost/app"\ndb = Database({url})\n'
    )

    assert load_project(tmp_path).dialect == dialect


@pytest.mark.parametrize(
    ("pyproject", "problem"),
    [
        (None, "no code under"),
        ("[project]\nname = 'app'\n", "no code under"),
        (
            "[tool.sqlakit.templates]\npath = ['sql']\n",
            "has path, and takes dialect, macros, namespace, paths",
        ),
    ],
)
def test_a_project_with_no_templates_to_find_is_refused(
    tmp_path: Path, pyproject: str | None, problem: str
) -> None:
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    with pytest.raises(ProjectConfigError, match=re.escape(problem)):
        load_project(tmp_path)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    for name, source in APP.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def test_a_project_is_read_from_its_code_without_running_it(app: Path) -> None:
    project = load_project(app / "shop")
    templates = project.templates
    assert (project.root, templates.paths, templates.namespace) == (
        app,
        (app / "shop" / "sql",),
        "q",
    )
    assert [
        signature_of(templates.macros[name], "q")
        for name in ("owned", "sided", "for_team")
    ] == [
        "q.owned(:team, *columns)",
        "q.sided([which])",
        "q.for_team(t)",
    ]
    assert templates.macros["owned"].doc == "Rows of the team."
    assert templates.macros["owned"].optional
    assert templates.macros["sided"].slots[0].choices == ("'left'", "'right'")
    assert "shop.macros" not in sys.modules
    assert "shop.db" not in sys.modules


def test_the_sql_directories_stand_in_for_code_that_says_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "app" / "sql").mkdir(parents=True)
    (tmp_path / "tests" / "sql").mkdir(parents=True)
    assert load_project(tmp_path).templates.paths == (tmp_path / "app" / "sql",)


def test_check_names_every_problem_once_where_it_is(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    available = ", ".join(sorted([*load_project(project).templates.macros, "include"]))
    assert main(["check"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        *FOUND,
        "",
        (
            "sql/inner.sql:2:7: Unknown macro tpl.nope in inner.sql:2; "
            f"available: {available}. Register one with "
            "`Templates(..., macros=[...])`."
        ),
        "sql/open.sql:2:3: open.sql:2: a quoted string is never closed.",
        "4 templates, 2 problems",
    ]


def test_check_writes_json(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--format", "json"]) == 1
    found = json.loads(capsys.readouterr().out)
    assert [(one["path"], one["line"], one["column"]) for one in found] == [
        ("sql/inner.sql", 2, 7),
        ("sql/open.sql", 2, 3),
    ]


def test_check_names_a_missing_include_where_the_include_is(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "sql" / "inner.sql").unlink()
    (project / "sql" / "open.sql").unlink()
    assert main(["check"]) == 1
    assert (
        capsys.readouterr()
        .out.splitlines()[len(FOUND) + 1]
        .startswith("sql/outer.sql:2:6: tpl.include: No SQL template named `inner.sql`")
    )


def test_check_passes_a_clean_project(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("inner.sql", "outer.sql", "open.sql"):
        (project / "sql" / name).unlink()
    assert main(["check"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        *FOUND,
        "",
        "1 template, 0 problems",
    ]


def test_check_says_when_the_project_says_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--project", str(tmp_path)]) == 2
    assert "Cannot read the project's templates" in capsys.readouterr().out


def test_a_broken_sql_macro_is_placed_on_its_line_below_the_select(
    project: Path,
) -> None:
    # A macro above it, so its lines count from where it stands in the file.
    source = (
        "-- Rows of the team.\n"
        "SELECT\n"
        "    t.team = :team\n"
        "    AND t.active AS for_team\n"
        "FROM t;\n"
        "\n"
        "-- Rows near a city.\n"
        "SELECT\n"
        "    tpl.if_set(\n"
        "        :filters.radius,\n"
        "        tpl.nope(:filters.cities)\n"
        "    ) AS near\n"
        "FROM t;\n"
    )
    macros = project / "_macros.sql"

    found = load_project(project).macro_problems(macros, source)

    assert [line for line, _ in found] == [
        source.splitlines().index("        tpl.nope(:filters.cities)") + 1
    ]


def test_check_names_a_broken_sql_macro(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("inner.sql", "outer.sql", "open.sql"):
        (project / "sql" / name).unlink()
    (project / "_macros.sql").write_text(SQL_MACROS.replace("t.public", "tpl.nope(t)"))
    assert main(["check"]) == 1
    assert (
        capsys.readouterr()
        .out.splitlines()[len(FOUND) + 1]
        .startswith("_macros.sql:4:1: Unknown macro tpl.nope in _macros.sql:4")
    )


def test_export_writes_what_sqruff_needs_into_pyproject(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "sql" / "dotted.sql").write_text(
        "SELECT * FROM users WHERE team = :c.team LIMIT :limit"
    )
    assert main(["export", "sqruff", "--check"]) == 1
    assert main(["export", "sqruff", "--dialect", "snowflake"]) == 0
    assert main(["export", "sqruff", "--check"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert written == PYPROJECT + (
        "\n"
        "[tool.sqruff.core]\n"
        'dialect = "snowflake"\n'
        'templater = "placeholder"\n'
        'rules = "all"\n'
        'exclude_rules = "RF01,RF02,RF03,AL05,ST03"\n'
        "\n"
        "[tool.sqruff.templater.placeholder]\n"
        'param_style = "colon"\n'
        'limit = "1"\n'
    )
    assert main(["export", "sqruff"]) == 0
    assert (project / "pyproject.toml").read_text() == written
    assert capsys.readouterr().out.splitlines()[-1] == "wrote pyproject.toml"


def test_export_keeps_what_is_yours(project: Path) -> None:
    (project / "pyproject.toml").write_text(
        PYPROJECT
        + '\n[tool.sqruff.core]\ndialect = "postgres"\nrules = "core"\n'
        + '\n[tool.sqruff.templater.placeholder]\nparam_style = "colon"\nold = "1"\n'
        + "\n[tool.other]\nkept = true\n"
    )
    assert main(["export", "sqruff"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert 'dialect = "postgres"\nrules = "core"\n' in written
    assert 'old = "1"' in written
    assert written.endswith('old = "1"\n\n[tool.other]\nkept = true\n')
    assert main(["export", "sqruff", "--check"]) == 0


def test_sqruff_reads_a_template_with_what_export_wrote(project: Path) -> None:
    assert main(["export", "sqruff", "--dialect", "postgres"]) == 0
    sqruff = shutil.which("sqruff")
    assert sqruff is not None
    ran = subprocess.run(  # noqa: S603 - the linter the project installs
        [sqruff, "lint", "--parsing-errors", "sql/good.sql", "_macros.sql"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Unparsable" not in ran.stdout + ran.stderr


def test_every_rule_passes_a_macro_that_takes_a_table(project: Path) -> None:
    # `tpl.for_team(u)` passes a table, which RF02 and RF03 read as a column.
    (project / "sql" / "team.sql").write_text(
        "SELECT\n    u.id,\n    u.name\nFROM users AS u\nWHERE tpl.for_team(u)\n"
    )
    # The settings export writes turn on every rule.
    assert main(["export", "sqruff", "--dialect", "postgres"]) == 0
    sqruff = shutil.which("sqruff")
    assert sqruff is not None
    # JSON, the one report of the same shape on GitHub Actions as anywhere.
    ran = subprocess.run(  # noqa: S603 - the linter the project installs
        [sqruff, "lint", "--format", "json", "sql/team.sql"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(ran.stdout or ran.stderr)
    codes = {problem["code"] for found in report.values() for problem in found}
    assert not {code for code in codes if code and code.startswith("RF")}, codes


def test_export_gives_a_value_to_a_word_the_dialect_keeps(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SQLAlchemy keeps neither word, and sqruff's SQLite keeps both.
    (project / "sql" / "kept.sql").write_text(
        "SELECT * FROM t WHERE tpl.in_list(t.status, :statuses, :exclude)\n"
        "  AND t.kind = :row.kind\n"
    )
    assert main(["export", "sqruff", "--dialect", "sqlite"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert 'exclude = "1"\n' in written
    assert 'row = "row_"\n' in written
    sqruff = shutil.which("sqruff")
    assert sqruff is not None
    ran = subprocess.run(  # noqa: S603 - the linter the project installs
        [sqruff, "lint", "--parsing-errors", "sql/kept.sql"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Unparsable" not in ran.stdout + ran.stderr

    # Without sqruff, the words SQLAlchemy keeps decide, and the check agrees.
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert main(["export", "sqruff", "--check"]) == 0


def test_export_writes_what_sqlfluff_needs_into_pyproject(project: Path) -> None:
    (project / "sql" / "dotted.sql").write_text(
        "SELECT * FROM users WHERE team = :c.team LIMIT :limit"
    )
    assert main(["export", "sqlfluff", "--check"]) == 1
    assert main(["export", "sqlfluff", "--dialect", "postgresql"]) == 0
    assert main(["export", "sqlfluff", "--check"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert written == PYPROJECT + (
        "\n"
        "[tool.sqlfluff.core]\n"
        'dialect = "postgres"\n'
        'templater = "placeholder"\n'
        'exclude_rules = "RF01,RF02,RF03,AL05,ST03"\n'
        "\n"
        "[tool.sqlfluff.templater.placeholder]\n"
        'param_style = "colon"\n'
        'limit = "1"\n'
    )
    # One linter's settings leave the other's alone.
    assert main(["export", "sqruff"]) == 0
    both = (project / "pyproject.toml").read_text()
    assert both.startswith(written)
    assert "[tool.sqruff.core]" in both
    assert main(["export", "sqlfluff", "--check"]) == 0


def test_sqlfluff_reads_a_template_with_what_export_wrote(project: Path) -> None:
    # sqlfluff's SQLite keeps `exclude` and `row`, and `tpl.for_team(u)` passes
    # a table, which RF02 and RF03 read as a column.
    (project / "sql" / "kept.sql").write_text(
        "SELECT u.id\nFROM users AS u\n"
        "WHERE tpl.for_team(u) AND tpl.in_list(u.status, :statuses, :exclude)\n"
        "    AND u.kind = :row.kind\n"
    )
    assert main(["export", "sqlfluff", "--dialect", "sqlite"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert 'exclude = "1"\n' in written
    assert 'row = "row_"\n' in written
    sqlfluff = shutil.which("sqlfluff")
    assert sqlfluff is not None
    ran = subprocess.run(  # noqa: S603 - the linter the project installs
        [sqlfluff, "lint", "--format", "json", "sql/kept.sql", "sql/good.sql"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    codes = {
        problem["code"]
        for found in json.loads(ran.stdout)
        for problem in found["violations"]
    }
    assert "PRS" not in codes
    assert not {code for code in codes if code.startswith("RF")}, codes


LINTED_MACROS = '''
from sqlakit.sql import Param, Sql, sql_macro


@sql_macro
def known(column: Sql) -> str:
    """Rows where the column is set."""
    return f"{column} IS NOT NULL"


@sql_macro
def bare(ids: Param) -> str:
    """Rows of the ids, written without the brackets `IN` takes."""
    return f"id IN {ids}"


@sql_macro
def half(q: Param) -> str:
    """Rows of the text, broken when it is given."""
    return "name = (" if q.value else "TRUE"
'''


@pytest.fixture
def linted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project on PostgreSQL whose Python macros write SQL, broken or not."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "db.py").write_text(
        DB.replace('os.environ["DATABASE_URL"]', '"postgresql://x/y"').replace(
            'macros=[HERE / "_macros.sql"]', 'macros=["linted_macros"]'
        )
    )
    (tmp_path / "linted_macros.py").write_text(LINTED_MACROS)
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "known.sql").write_text(
        "SELECT id FROM users WHERE tpl.known(name)\n"
    )
    (tmp_path / "sql" / "bare.sql").write_text(
        "SELECT name\nFROM users\nWHERE tpl.bare(:ids)\n"
    )
    (tmp_path / "sql" / "half.sql").write_text(
        "SELECT id FROM users WHERE tpl.half(:q)\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "linted_macros", raising=False)
    return tmp_path


def test_check_lints_the_sql_the_macros_render(
    linted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["export", "sqruff"]) == 0
    capsys.readouterr()

    assert main(["check", "--lint", "sqruff"]) == 1

    lines = capsys.readouterr().out.splitlines()
    assert lines[-7:] == [
        (
            "sql/bare.sql:3:7: tpl.bare writes SQL sqruff cannot parse, with every "
            "parameter given and with none given"
        ),
        "    WHERE id IN :ids",
        "             ^",
        (
            "sql/half.sql:1:28: tpl.half writes SQL sqruff cannot parse, with every "
            "parameter given"
        ),
        "    SELECT id FROM users WHERE name = (",
        "                                      ^",
        "3 templates, 2 problems",
    ]


def test_check_lints_with_sqlfluff_too(linted: Path) -> None:
    assert main(["export", "sqlfluff"]) == 0

    assert main(["check", "--lint", "sqlfluff", "--format", "json"]) == 1


def test_check_says_what_it_needs_to_lint(
    linted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--lint", "sqruff"]) == 2
    assert capsys.readouterr().out.strip() == (
        "the `sqruff` settings do not read the templates: run "
        "`sqlakit export sqruff` first"
    )
    assert main(["export", "sqruff"]) == 0
    capsys.readouterr()
    assert main(["check", "--lint", "sqruff", "--dialect", "sqlite"]) == 2
    assert capsys.readouterr().out.strip() == (
        "the templates render on sqlite, and `sqruff` reads postgresql: pass "
        "--dialect postgresql, or set the dialect in `[tool.sqruff.core]`"
    )


def test_render_writes_each_template_both_ways(
    linted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["render", "--out", "rendered"]) == 0

    assert capsys.readouterr().out.strip() == "wrote 6 files to rendered"
    assert (linted / "rendered" / "half.given.sql").read_text() == (
        "-- half.sql on postgresql, every parameter given\n"
        "SELECT id FROM users WHERE name = (\n"
    )
    assert (linted / "rendered" / "half.not_given.sql").read_text() == (
        "-- half.sql on postgresql, no parameter given\n"
        "SELECT id FROM users WHERE TRUE\n"
    )


def test_export_writes_what_sql_formatter_needs(project: Path) -> None:
    assert main(["export", "sql-formatter", "--check"]) == 1
    # The project names no dialect, so the language is plain SQL.
    assert main(["export", "sql-formatter"]) == 0
    assert main(["export", "sql-formatter", "--check"]) == 0
    assert json.loads((project / ".sql-formatter.json").read_text()) == {
        "language": "sql",
        "paramTypes": {"named": [":"]},
    }
    (project / ".sql-formatter.json").unlink()
    assert main(["export", "sql-formatter", "--dialect", "oracle"]) == 0
    assert json.loads((project / ".sql-formatter.json").read_text())["language"] == (
        "plsql"
    )


def test_export_keeps_the_sql_formatter_settings_that_are_yours(
    project: Path,
) -> None:
    (project / ".sql-formatter.json").write_text(
        '{"language": "postgresql", "keywordCase": "upper",'
        ' "paramTypes": {"named": ["@"], "positional": true}}'
    )
    assert main(["export", "sql-formatter", "--check"]) == 1
    assert main(["export", "sql-formatter", "--dialect", "sqlite"]) == 0
    assert json.loads((project / ".sql-formatter.json").read_text()) == {
        "language": "postgresql",
        "keywordCase": "upper",
        "paramTypes": {"named": ["@", ":"], "positional": True},
    }
    (project / ".sql-formatter.json").write_text("{not json")
    assert main(["export", "sql-formatter"]) == 2
    assert main(["export", "sql-formatter", "--check"]) == 1


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx is not installed")
def test_sql_formatter_keeps_a_template_whole_with_what_export_wrote(
    project: Path,
) -> None:
    source = (
        "select u.id from users as u where tpl.if_set(:q, u.name = :q) and u.team ="
        " :team.id and u.age > :limit::int order by tpl.order_by(:sort, id, name)"
        " limit :page_size\n"
    )
    (project / "sql" / "formatted.sql").write_text(source)
    assert main(["export", "sql-formatter", "--dialect", "postgresql"]) == 0
    npx = shutil.which("npx")
    assert npx is not None
    ran = subprocess.run(  # noqa: S603 - the formatter, run the way its README says
        [npx, "-y", "sql-formatter@15", "sql/formatted.sql"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = load_project(project)
    before = loaded.load("formatted.sql", source)
    after = loaded.load("formatted.sql", ran.stdout)
    assert (
        after.parameters()
        == before.parameters()
        == {
            "q",
            "team",
            "limit",
            "sort",
            "page_size",
        }
    )
    assert ":limit::int" in ran.stdout
    assert "tpl.if_set" in ran.stdout


def test_check_passes_a_file_of_macros_among_the_templates(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (app / "shop" / "sql" / "_macros.sql").write_text(
        "SELECT q.if_set(negate, NOT col, col) AS flipped FROM col, negate;\n"
        "\n"
        "-- Rows of the team.\n"
        "SELECT t.team = :team AS for_team FROM t;\n"
    )
    assert main(["check", "--project", str(app)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "templates: shop/sql (shop/db.py:11)",
        "namespace: q (shop/db.py:11)",
        "macros: 2 in Python, 1 file of SQL macros",
        "dialect: sqlite (shop/db.py:9)",
        "",
        "1 template, 0 problems",
    ]


def test_export_gives_a_stage_and_a_sample_the_value_a_linter_reads(
    project: Path,
) -> None:
    (project / "sql" / "copy.sql").write_text(
        "COPY INTO orders FROM :location;\n"
        "LIST :listed;\n"
        "COPY INTO :target\nFROM (SELECT * FROM t SAMPLE (:percent) LIMIT :limit)\n"
        "HEADER = TRUE;\n"
        "COPY INTO :table FROM @stage;\n"
    )
    assert load_project(project).placeholder_values() == {
        "limit": "1",
        "listed": "@stage/path",
        "location": "@stage/path",
        "percent": "10",
        "table": "table_",
        "target": "@stage/path",
    }


def test_a_file_macro_is_read_from_the_code_with_its_file(app: Path) -> None:
    (app / "shop" / "sql" / "tenant.sql").write_text(
        "SELECT t.tenant_id = :tenant_id AS for_tenant FROM t;\n"
    )
    (app / "shop" / "tenant.py").write_text(
        "from sqlakit.sql import Sql, sql_macro\n\n\n"
        '@sql_macro("sql/tenant.sql")\n'
        "def for_tenant(t: Sql) -> dict:\n"
        '    return {"tenant_id": 1}\n'
    )
    templates = load_project(app).templates
    macro = templates.macros["for_tenant"]
    assert getattr(macro, "sql_path", None) == app / "shop" / "sql" / "tenant.sql"
    assert templates.names() == ["users.sql"]


def test_a_call_under_tpl_is_marked_when_the_namespace_is_another(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "q.sql").write_text(
        "SELECT 1 WHERE t.if_set(:a, TRUE) AND tpl.if_set(:b, TRUE) -- tpl.when(\n"
    )
    (tmp_path / "db.py").write_text('Templates("sql", namespace="t")\n')

    assert main(["check", "--project", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines()[5:] == [
        (
            f"{tmp_path / 'sql' / 'q.sql'}:1:39: `tpl.if_set` is not a macro call: "
            "the namespace is `t`, so write `t.if_set`"
        ),
        "1 template, 1 problem",
    ]


def test_a_call_that_cannot_be_made_stays_a_call_when_asked(project: Path) -> None:
    from sqlakit._sql import Context, calls_kept

    loaded = load_project(project)
    template = loaded.load(
        "x.sql",
        "SELECT * FROM tpl.values(:rows) AS v\n"
        "WHERE tpl.if_set(:q, tpl.mine(:teams)) AND tpl.if_set(:b, b = :b)",
    )
    values = {"q": 1, "teams": [1], "b": None, "rows": None}
    ctx = Context("postgresql", postgresql.dialect().identifier_preparer, values)

    with pytest.raises(MacroArgumentError, match="has no rows"):
        template.render(ctx)
    with calls_kept():
        assert template.render(ctx) == (
            "SELECT * FROM tpl.values(:rows) AS v\nWHERE tpl.mine(:teams) AND TRUE"
        )


@pytest.mark.parametrize(
    ("dialect", "written"),
    [
        ("postgresql", "postgres"),
        ("mariadb", "mysql"),
        ("mssql", "tsql"),
        ("sqlite", "sqlite"),
    ],
)
def test_export_writes_the_dialect_by_the_name_sqruff_knows(
    project: Path, dialect: str, written: str
) -> None:
    assert main(["export", "sqruff", "--dialect", dialect]) == 0
    assert f'dialect = "{written}"' in (project / "pyproject.toml").read_text()


def test_export_rewrites_a_table_whose_header_carries_a_comment(project: Path) -> None:
    (project / "pyproject.toml").write_text(
        PYPROJECT + '\n[tool.sqruff.templater.placeholder]  # mine\nkept = "1"\n'
    )
    assert main(["export", "sqruff"]) == 0

    written = (project / "pyproject.toml").read_text()

    assert written.count("[tool.sqruff.templater.placeholder]") == 1
    assert 'kept = "1"' in written
    tomllib.loads(written)


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (
            lambda root: (root / "_macros.sql").write_text("SELECT broken;\n"),
            "`_macros.sql` cannot be a macro: line 1 is not",
        ),
        (
            lambda root: (root / "twice.py").write_text(MACROS),
            "`mine` cannot be a macro: another macro has that name",
        ),
        (
            lambda root: (root / "pyproject.toml").write_text("[project\n"),
            "is not TOML",
        ),
        (
            lambda root: (root / "pyproject.toml").write_text(
                PYPROJECT + '[tool.sqlakit.templates]\npaths = "sql"\n'
            ),
            'takes a list of strings as `paths`, such as ["app/sql"]',
        ),
        (
            lambda root: (root / "pyproject.toml").write_text(
                PYPROJECT + '[tool.sqlakit.templates]\npaths = ["nope"]\n'
            ),
            "nope` is not a directory",
        ),
    ],
)
def test_a_project_that_cannot_be_read_is_said_and_exits_2(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], object],
    said: str,
) -> None:
    change(project)

    assert main(["check"]) == 2
    assert said in capsys.readouterr().out
    assert main(["export", "sqruff"]) == 2
    assert main(["export", "pycharm"]) == 2


def test_check_with_no_template_to_check_exits_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for template in (project / "sql").glob("*.sql"):
        template.unlink()

    assert main(["check", "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"].startswith(
        "no template to check under"
    )


def test_a_template_that_is_not_utf8_is_a_problem(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "sql" / "latin.sql").write_bytes("SELECT 1 -- café".encode("cp1252"))

    assert main(["check"]) == 1
    assert "sql/latin.sql:1:1: The file is not UTF-8 text." in capsys.readouterr().out


@pytest.mark.parametrize(
    "spelled",
    [
        'Templates(Path("queries"))',
        'Templates(BASE / Path("queries"))',
        'Templates(BASE / "queries")',
    ],
)
def test_a_path_is_read_however_the_code_spells_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelled: str
) -> None:
    (tmp_path / "queries").mkdir()
    (tmp_path / "db.py").write_text(
        f"from pathlib import Path\n\nBASE = Path(__file__).parent\n{spelled}\n"
    )
    monkeypatch.chdir(tmp_path.parent)

    assert load_project(tmp_path).templates.paths == (tmp_path / "queries",)


def test_the_dialect_is_the_one_of_the_database_with_the_templates(
    tmp_path: Path,
) -> None:
    (tmp_path / "sql").mkdir()
    (tmp_path / "conftest.py").write_text('Database("sqlite://")\n')
    (tmp_path / "a.py").write_text('Database("mysql://x/y")\n')
    (tmp_path / "b.py").write_text(
        'Database("postgresql://x/y", templates=Templates("sql"))\n'
    )

    assert load_project(tmp_path).dialect == "postgresql"


def test_the_namespace_is_guessed_in_the_sql_directories_found(
    tmp_path: Path,
) -> None:
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "q.sql").write_text("SELECT 1 WHERE q.if_set(:a, TRUE)")
    (tmp_path / "db.py").write_text(
        "import settings\nTemplates(settings.SQL_DIR, namespace=settings.NAMESPACE)\n"
    )

    assert load_project(tmp_path).templates.namespace == "q"


def test_the_macros_of_a_project_module_list_from_its_root(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(app)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(app))])

    assert main(["macros", "shop.macros"]) == 0
    assert "q.owned(:team, *columns)" in capsys.readouterr().out
