"""The database a model uses, and who decides."""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import sqlakit
from sqlakit import (
    DEFAULT_ALIAS,
    AliasInUseError,
    Database,
    DefaultAliasError,
    MissingRegistryError,
    MissingSessionError,
    Router,
    UnknownDatabaseError,
    UnknownImportPathError,
    UnregisteredDatabaseError,
)
from sqlakit._model import db_for
from sqlakit.orm import ModelMixin, Query


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Warehoused(Base):
    """Placement, said once, for every model under it."""

    __abstract__ = True
    __db__ = "warehouse"


class Event(Warehoused):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    what: Mapped[str]


@pytest.fixture
def databases() -> Iterator[None]:
    sqlakit.db.configure(
        {
            "default": {"url": "sqlite://"},
            "replica": {"url": "sqlite://"},
            "warehouse": {"url": "sqlite://"},
        }
    )
    for alias in sqlakit.db.aliases:
        with sqlakit.db[alias].transaction() as conn:
            Base.metadata.create_all(conn)
    yield
    sqlakit.db.dispose()


def _write(alias: str, name: str) -> None:
    with sqlakit.db[alias].transaction() as conn:
        conn.execute(sa.text(f"INSERT INTO users VALUES (1, '{name}')"))  # noqa: S608


# where a model lives


def test_a_model_lives_on_the_default_database(databases: None) -> None:
    assert User.db is sqlakit.db["default"]


def test_a_base_class_places_every_model_under_it(databases: None) -> None:
    assert Event.db is sqlakit.db["warehouse"]
    assert User.db is sqlakit.db["default"]


@pytest.fixture
def bare() -> Iterator[None]:
    """Configured, with nothing created yet."""
    sqlakit.db.configure(
        {
            "default": {
                "url": "sqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
            "warehouse": {
                "url": "sqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
        }
    )
    yield
    sqlakit.db.dispose()


def test_provisioning_follows_that_placement(bare: None) -> None:
    with Base.provisioned_tables(), Base.provisioned_tables("warehouse"):
        with sqlakit.db["default"].connect() as conn:
            assert sa.inspect(conn).get_table_names() == ["users"]
        with sqlakit.db["warehouse"].connect() as conn:
            assert sa.inspect(conn).get_table_names() == ["events"]


# a block somewhere else


def test_a_block_stands_in_for_the_default_database(databases: None) -> None:
    _write("replica", "replica")

    with sqlakit.db.using("replica").connect():
        assert User.db is sqlakit.db["replica"]
        assert User.query.one().name == "replica"

    assert User.db is sqlakit.db["default"]


def test_a_block_leaves_a_model_that_lives_elsewhere_alone(databases: None) -> None:
    with sqlakit.db.using("replica").connect():
        assert User.db is sqlakit.db["replica"]
        assert Event.db is sqlakit.db["warehouse"]


def test_a_block_can_redirect_without_opening_anything(databases: None) -> None:
    _write("replica", "replica")

    with sqlakit.db.using("replica"), sqlakit.db["replica"].connect():
        assert User.query.one().name == "replica"


def test_the_redirection_ends_with_the_block_it_failed_in(databases: None) -> None:
    with pytest.raises(ZeroDivisionError), sqlakit.db.using("replica").connect():
        assert User.db is sqlakit.db["replica"]
        _ = 1 / 0

    assert User.db is sqlakit.db["default"]


def test_a_transaction_of_another_database_writes_there(databases: None) -> None:
    with sqlakit.db.using("warehouse").transaction():
        Event(id=1, what="moved in").save()

    with sqlakit.db["warehouse"].connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM events")) == 1


def test_a_block_refuses_a_database_nobody_configured(databases: None) -> None:
    with pytest.raises(UnknownDatabaseError):
        sqlakit.db.using("nowhere")


def test_a_block_takes_the_database_itself(databases: None) -> None:
    _write("replica", "replica")
    replica = sqlakit.db["replica"]

    with sqlakit.db.using(replica).connect():
        assert User.db is replica
        assert User.query.one().name == "replica"

    assert sqlakit.db.using(sqlakit.db["default"]).url == sqlakit.db["default"].url


def test_a_block_refuses_a_database_the_registry_does_not_hold(
    databases: None,
) -> None:
    with pytest.raises(UnregisteredDatabaseError):
        sqlakit.db.using(Database("sqlite://"))


def test_the_handle_is_the_database_it_stands_for(databases: None) -> None:
    assert sqlakit.db.using("replica").url == sqlakit.db["replica"].url
    assert "replica" in repr(sqlakit.db.using("replica"))


# a query somewhere else


def test_a_query_may_name_its_own_database(databases: None) -> None:
    _write("default", "primary")
    _write("replica", "replica")

    with sqlakit.db["default"].transaction(), sqlakit.db["replica"].connect():
        assert User.query.using("replica").one().name == "replica"
        assert User.query.one().name == "primary"


def test_a_query_takes_a_database_as_well_as_a_name() -> None:
    other = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with other.transaction() as conn:
        Base.metadata.create_all(conn)
        conn.execute(sa.text("INSERT INTO users VALUES (1, 'elsewhere')"))

    with other.connect():
        assert User.query.using(other).one().name == "elsewhere"

    other.dispose()


def test_a_query_still_runs_in_a_block(databases: None) -> None:
    with sqlakit.db["default"].connect(), pytest.raises(MissingSessionError):
        # Naming a database chooses one; it does not open anything.
        User.query.using("replica").all()


# a source of its own, instead of the importable registry


def test_a_model_may_look_its_alias_up_somewhere_else() -> None:
    main = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})

    class Standalone(ModelMixin, DeclarativeBase):
        __dbs__ = {"main": main}  # noqa: RUF012 - typed by the base, as any source
        __db__ = "main"

    class Ticket(Standalone):
        __tablename__ = "tickets"

        id: Mapped[int] = mapped_column(primary_key=True)

    with main.transaction() as conn:
        Standalone.metadata.create_all(conn)

    assert Ticket.db is main

    with main.transaction():
        assert Ticket.query.count() == 0

    main.dispose()


def test_a_source_of_its_own_may_hand_over_the_database_itself() -> None:
    main = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})

    class Held(ModelMixin, DeclarativeBase):
        __dbs__ = {"main": main}  # noqa: RUF012 - typed by the base, as any source
        __db__ = main  # the database itself, not a name to look up

    assert Held.db is main

    main.dispose()


def test_a_model_with_no_source_cannot_resolve_a_name() -> None:
    class Loose:
        __db__ = "main"

    with pytest.raises(MissingRegistryError, match="`Loose` has no registry"):
        db_for(Loose)


def test_a_query_on_a_model_with_no_source_cannot_take_an_alias() -> None:
    class Plain(DeclarativeBase):
        pass

    class Row(Plain):
        __tablename__ = "rows"

        id: Mapped[int] = mapped_column(primary_key=True)

    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})

    with pytest.raises(MissingRegistryError, match="Pass the database itself"):
        Query(Row, db).using("replica")

    assert Query(Row, db).using(db).db is db


def test_every_kind_of_block_redirects_while_it_is_open(databases: None) -> None:
    with sqlakit.db.using("replica").autocommit():
        assert User.db is sqlakit.db["replica"]

    with sqlakit.db.using("replica").session_factory():
        assert User.db is sqlakit.db["replica"]

    assert User.db is sqlakit.db["default"]


def test_a_block_that_cannot_open_leaves_nothing_behind() -> None:
    sqlakit.db.configure(
        {
            "default": {"url": "sqlite://"},
            "broken": {"url": "sqlite:////nowhere/at/all.db"},
        }
    )

    with pytest.raises(sa.exc.OperationalError), sqlakit.db.using("broken").connect():
        pass  # pragma: no cover

    assert User.db is sqlakit.db["default"]

    sqlakit.db.dispose()


# a policy, for models that cannot say it themselves


def in_the_warehouse(model: type) -> str | None:
    return "warehouse" if model is Event else None


def test_a_router_places_a_model(databases: None) -> None:
    Event.__db__ = DEFAULT_ALIAS  # it says nothing of its own here
    sqlakit.db.route(in_the_warehouse)

    assert Event.db is sqlakit.db["warehouse"]
    assert User.db is sqlakit.db["default"]

    sqlakit.db.route()
    Event.__db__ = "warehouse"

    assert Event.db is sqlakit.db["warehouse"]


def test_the_routers_are_asked_in_order(databases: None) -> None:
    sqlakit.db.route(lambda _model: None, lambda _model: "replica")

    assert User.db is sqlakit.db["replica"]
    assert len(sqlakit.db.routers) == 2

    sqlakit.db.route()

    assert User.db is sqlakit.db["default"]


def test_configure_takes_them_by_name() -> None:
    sqlakit.db.configure(
        {"default": {"url": "sqlite://"}, "warehouse": {"url": "sqlite://"}},
        routers=["tests.unit.test_routing.in_the_warehouse"],
    )

    assert Event.db is sqlakit.db["warehouse"]
    assert User.db is sqlakit.db["default"]

    sqlakit.db.route()
    sqlakit.db.dispose()


def test_a_name_that_leads_nowhere_says_so() -> None:
    # A module of this project's own: a name like `app.db` is one a working
    # tree may hold, and the check imports it.
    path = "tests.unit.test_routing.no_such_router"

    with pytest.raises(UnknownImportPathError, match=path.replace(".", r"\.")):
        sqlakit.db.configure("sqlite://", routers=[path])

    sqlakit.db.route()


def test_a_model_of_its_own_wins_over_the_policy(databases: None) -> None:
    sqlakit.db.route(lambda _model: "replica")

    # `__db__` is asked only when no router answers, as Django's are.
    assert User.db is sqlakit.db["replica"]

    sqlakit.db.route()


def test_a_router_may_be_a_class(databases: None) -> None:
    class Placement(Router):
        def db_for(self, model: type) -> str | None:
            return "warehouse" if model is Event else None

    Event.__db__ = DEFAULT_ALIAS
    sqlakit.db.route(Placement())

    assert Event.db is sqlakit.db["warehouse"]
    assert User.db is sqlakit.db["default"]

    sqlakit.db.route()
    Event.__db__ = "warehouse"


def test_a_router_that_names_a_database_nobody_configured(databases: None) -> None:
    sqlakit.db.route(lambda _model: "nowhere")

    with pytest.raises(UnknownDatabaseError):
        _ = User.db

    sqlakit.db.route()


def test_the_routers_are_cleared_by_route_with_nothing(databases: None) -> None:
    sqlakit.db.route(lambda _model: "warehouse")
    sqlakit.db.route()

    assert User.db is sqlakit.db["default"]


def test_the_inner_block_decides_while_it_is_open(databases: None) -> None:
    with sqlakit.db.using("replica"):
        with sqlakit.db.using("warehouse"):
            assert User.db is sqlakit.db["warehouse"]

        assert User.db is sqlakit.db["replica"]


def test_a_model_that_lives_elsewhere_still_needs_a_block(databases: None) -> None:
    # `using` chooses a database, it does not open one.
    with sqlakit.db.using("replica").transaction(), pytest.raises(MissingSessionError):
        Event(id=1, what="nowhere to write it").save()


def test_a_router_wins_over_the_block(databases: None) -> None:
    sqlakit.db.route(lambda model: "warehouse" if model is User else None)

    with sqlakit.db.using("replica"):
        assert User.db is sqlakit.db["warehouse"]

    sqlakit.db.route()


def test_a_template_is_not_a_model_so_no_router_reaches_it(databases: None) -> None:
    sqlakit.db.route(lambda _model: "warehouse")
    _write("replica", "on the replica")

    with sqlakit.db["replica"].connect():
        rows = sqlakit.db["replica"].sql.from_string("SELECT name FROM users").scalars()

        assert rows.all() == ["on the replica"]

    sqlakit.db.route()


# a registry of the model's own


@pytest.fixture
def own() -> Iterator[type[ModelMixin]]:
    class OwnBase(ModelMixin, DeclarativeBase):
        pass

    class Note(OwnBase):
        __tablename__ = "notes"

        id: Mapped[int] = mapped_column(primary_key=True)
        text: Mapped[str]

    OwnBase.register_db(Database("sqlite://"), alias="db1")
    OwnBase.register_db(Database("sqlite://"), alias="db2")
    for alias in ("db1", "db2"):
        with OwnBase.dbs[alias].transaction() as conn:
            OwnBase.metadata.create_all(conn)
    yield Note
    OwnBase.dbs.dispose()


def test_register_db_leaves_the_importable_registry_alone(own: type[Any]) -> None:
    assert own.dbs is not sqlakit.db
    assert own.dbs.aliases == (DEFAULT_ALIAS, "db1", "db2")


def test_a_registered_alias_takes_the_writes_and_the_reads(own: type[Any]) -> None:
    with own.dbs.using("db1").transaction():
        own(id=1, text="written to db1").save()
    with own.dbs.using("db2").transaction():
        own(id=1, text="written to db2").save()

    with own.dbs.using("db1").connect():
        assert [note.text for note in own.query.all()] == ["written to db1"]
    with own.dbs.using("db2").connect():
        assert [note.text for note in own.query.all()] == ["written to db2"]


def test_a_registered_alias_answers_a_query_that_names_it(own: type[Any]) -> None:
    with own.dbs.using("db2").transaction():
        own(id=1, text="written to db2").save()

    with own.dbs["db2"].connect():
        assert [note.text for note in own.query.using("db2").all()] == [
            "written to db2"
        ]


def test_an_alias_another_database_holds(own: type[Any]) -> None:
    with pytest.raises(AliasInUseError, match="db2"):
        own.register_db(Database("sqlite://"), alias="db2")


def test_using_moves_a_model_pinned_to_the_default_database(own: type[Any]) -> None:
    """`set_db(db)` and `register_db(db)` follow a redirect the same way."""
    default = Database("sqlite://")
    own.dbs.register(DEFAULT_ALIAS, default)
    own.set_db(default)

    assert own.db is default

    with own.dbs.using("db2"):
        assert own.db is own.dbs["db2"]

    assert own.db is default

    default.dispose()


def test_a_default_the_registry_did_not_build(own: type[Any]) -> None:
    default = Database("sqlite://")
    own.register_db(default, alias=DEFAULT_ALIAS)

    assert own.dbs[DEFAULT_ALIAS] is default
    assert own.db is default
    assert own.dbs.is_configured


def test_a_second_default_is_refused(own: type[Any]) -> None:
    own.register_db(Database("sqlite://"))

    with pytest.raises(DefaultAliasError, match=DEFAULT_ALIAS):
        own.register_db(Database("sqlite://"))


def test_a_model_under_one_registers_into_the_same_registry(own: type[Any]) -> None:
    own.register_db(Database("sqlite://"), alias="db3")

    assert own.dbs.aliases == (DEFAULT_ALIAS, "db1", "db2", "db3")


def test_register_db_with_no_alias_registers_the_default() -> None:
    class OneBase(ModelMixin, DeclarativeBase):
        pass

    class Note(OneBase):
        __tablename__ = "one_notes"

        id: Mapped[int] = mapped_column(primary_key=True)

    one = Database("sqlite://")
    OneBase.register_db(one)

    assert Note.db is one
    assert OneBase.dbs is not sqlakit.db  # the importable one is left alone
    assert OneBase.dbs[DEFAULT_ALIAS] is one

    one.dispose()
