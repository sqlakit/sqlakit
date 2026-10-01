"""A database found when the code runs, not when it is imported."""

import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import sqlakit
from sqlakit import (
    DEFAULT_ALIAS,
    Database,
    Databases,
    RetryingTransaction,
    UnknownDatabaseError,
    autocommit,
    transaction,
)
from sqlakit.orm import ModelMixin


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Event(Base):
    __tablename__ = "events"
    __db__ = "replica"

    id: Mapped[int] = mapped_column(primary_key=True)
    what: Mapped[str]


def _database() -> Database:
    """Return an in-memory database with the tables, kept on one connection."""
    database = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with database.transaction() as conn:
        Base.metadata.create_all(conn)
    return database


def _names(database: Database) -> list[str]:
    with database.connect() as conn:
        return list(conn.scalars(sa.text("SELECT name FROM users ORDER BY id")))


@pytest.fixture
def real() -> Iterator[Databases]:
    """The importable registry, on databases of its own."""
    sqlakit.db.configure(
        {
            "default": {
                "url": "sqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
            "replica": {
                "url": "sqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
        }
    )
    for alias in sqlakit.db.aliases:
        with sqlakit.db[alias].transaction() as conn:
            Base.metadata.create_all(conn)
    yield sqlakit.db
    sqlakit.db.dispose()


@pytest.fixture
def test_db() -> Iterator[Database]:
    with _database() as database:
        yield database


def _add(name: str) -> None:
    sqlakit.db.session.add(User(name=name))


# override


def test_the_default_database_is_another_for_the_block(
    real: Databases, test_db: Database
) -> None:
    with real.override(test_db) as overridden:
        assert overridden is test_db
        assert real[DEFAULT_ALIAS] is test_db
        assert User.db is test_db
        with real.transaction():
            _add("ada")
            User(name="grace").save()

    assert _names(test_db) == ["ada", "grace"]
    assert _names(real) == []
    assert real[DEFAULT_ALIAS] is real
    assert User.db is real


def test_another_alias_is_another_for_the_block(
    real: Databases, test_db: Database
) -> None:
    with real.override(test_db, alias="replica"):
        assert real["replica"] is test_db
        assert Event.db is test_db
        with Event.db.transaction():
            Event(what="moved").save()
        assert User.db is real

    with test_db.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM events")) == 1
    assert Event.db is real["replica"]
    assert Event.db is not test_db


def test_the_database_is_left_open_for_whoever_built_it(
    real: Databases, test_db: Database
) -> None:
    with real.override(test_db), real.transaction():
        _add("ada")

    assert test_db._engine is not None
    assert _names(test_db) == ["ada"]


def test_the_alias_comes_back_when_the_block_fails(
    real: Databases, test_db: Database
) -> None:
    with pytest.raises(ZeroDivisionError), real.override(test_db):
        _ = 1 / 0

    assert real[DEFAULT_ALIAS] is real


def test_an_inner_block_wins_and_the_outer_one_comes_back(
    real: Databases, test_db: Database
) -> None:
    with _database() as inner, real.override(test_db):
        with real.override(inner):
            assert real[DEFAULT_ALIAS] is inner
        assert real[DEFAULT_ALIAS] is test_db

    assert real[DEFAULT_ALIAS] is real


def test_an_alias_nobody_configured_exists_for_the_block(
    real: Databases, test_db: Database
) -> None:
    with real.override(test_db, alias="warehouse"):
        assert real["warehouse"] is test_db
        assert "warehouse" in real
        assert real.aliases == (DEFAULT_ALIAS, "replica", "warehouse")

    assert "warehouse" not in real
    assert real.aliases == (DEFAULT_ALIAS, "replica")
    with pytest.raises(UnknownDatabaseError):
        real["warehouse"]


def test_a_registry_nobody_configured_works_for_the_block(test_db: Database) -> None:
    registry = Databases()

    with registry.override(test_db):
        assert registry.is_configured is True
        assert repr(registry) == "Databases(Database('sqlite://'))"
        with registry.transaction():
            registry.session.add(User(name="ada"))

    assert registry.is_configured is False
    assert repr(registry) == "Databases(unconfigured)"
    assert _names(test_db) == ["ada"]


def test_a_registered_default_shows_in_the_repr(test_db: Database) -> None:
    registry = Databases()
    registry.register(DEFAULT_ALIAS, test_db)

    assert repr(registry) == "Databases(Database('sqlite://'))"


def test_the_registry_itself_puts_the_default_back(real: Databases) -> None:
    with real.override(real):
        assert real[DEFAULT_ALIAS] is real
        with real.transaction():
            _add("ada")

    assert _names(real) == ["ada"]


def test_every_thread_sees_it(real: Databases, test_db: Database) -> None:
    seen: list[Any] = []

    with real.override(test_db):
        thread = threading.Thread(target=lambda: seen.append(real[DEFAULT_ALIAS]))
        thread.start()
        thread.join()

    assert seen == [test_db]


def test_a_recording_says_the_alias_it_was_reached_under(
    real: Databases, test_db: Database
) -> None:
    test_db._name = "test"

    with real.override(test_db), real.recording() as recording, real.transaction():
        real.session.execute(sa.text("SELECT 1"))

    assert [statement.database for statement in recording.statements] == [DEFAULT_ALIAS]
    assert test_db._name == "test"


def test_using_reaches_the_database_under_the_alias_it_now_has(
    real: Databases, test_db: Database
) -> None:
    with real.override(test_db, alias="replica"):
        assert real.using(test_db).url == test_db.url
        with real.using("replica"):
            assert User.db is test_db


def test_a_model_pinned_to_the_default_database_follows(test_db: Database) -> None:
    class OwnBase(ModelMixin, DeclarativeBase):
        pass

    class Note(OwnBase):
        __tablename__ = "notes"

        id: Mapped[int] = mapped_column(primary_key=True)

    with Database("sqlite://") as default:
        OwnBase.register_db(default)
        OwnBase.set_db(default)

        with OwnBase.dbs.override(test_db):
            assert Note.db is test_db

        assert Note.db is default


def test_a_model_pinned_to_a_database_the_registry_does_not_hold_stays(
    real: Databases, test_db: Database
) -> None:
    class OwnBase(ModelMixin, DeclarativeBase):
        pass

    class Note(OwnBase):
        __tablename__ = "notes"

        id: Mapped[int] = mapped_column(primary_key=True)

    with Database("sqlite://") as elsewhere:
        OwnBase.set_db(elsewhere)

        with real.override(test_db):
            assert Note.db is elsewhere


# transaction


def test_transaction_finds_the_registry_when_it_is_called() -> None:
    @transaction
    def create(name: str) -> str:
        _add(name)
        return name

    sqlakit.db.configure("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with sqlakit.db.transaction() as conn:
        Base.metadata.create_all(conn)

    assert create("ada") == "ada"
    assert _names(sqlakit.db) == ["ada"]

    sqlakit.db.dispose()


def test_transaction_follows_an_override(real: Databases, test_db: Database) -> None:
    @transaction
    def create(name: str) -> None:
        _add(name)

    with real.override(test_db):
        create("ada")
    create("grace")

    assert _names(test_db) == ["ada"]
    assert _names(real) == ["grace"]


def test_transaction_takes_an_alias(real: Databases, test_db: Database) -> None:
    @transaction(using="replica")
    def create(name: str) -> None:
        real["replica"].session.add(User(name=name))

    create("ada")
    with real.override(test_db, alias="replica"):
        create("grace")

    assert _names(real["replica"]) == ["ada"]
    assert _names(test_db) == ["grace"]


def test_transaction_asks_a_callable_on_every_call(test_db: Database) -> None:
    asked: list[Database] = []

    def source() -> Database:
        asked.append(test_db)
        return test_db

    @transaction(using=source)
    def create(name: str) -> None:
        test_db.session.add(User(name=name))

    assert asked == []

    create("ada")
    create("grace")

    assert asked == [test_db, test_db]
    assert _names(test_db) == ["ada", "grace"]


def test_transaction_takes_the_database_itself(test_db: Database) -> None:
    @transaction(using=test_db)
    def create(name: str) -> None:
        test_db.session.add(User(name=name))

    create("ada")

    assert _names(test_db) == ["ada"]


def test_transaction_rolls_back_what_raised(test_db: Database) -> None:
    @transaction(using=test_db)
    def create(name: str) -> None:
        test_db.session.add(User(name=name))
        test_db.session.flush()
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        create("ada")

    assert _names(test_db) == []


def test_transaction_takes_the_options_of_database_transaction(
    test_db: Database,
) -> None:
    @transaction(using=test_db, rollback=True)
    def create(name: str) -> list[str]:
        test_db.session.add(User(name=name))
        test_db.session.flush()
        return list(test_db.session.scalars(sa.select(User.name)))

    assert create("ada") == ["ada"]
    assert _names(test_db) == []


def test_transaction_keeps_the_name_of_the_function() -> None:
    @transaction(using="replica")
    def create_user() -> None:  # pragma: no cover - never called
        """Create one."""

    assert create_user.__name__ == "create_user"
    assert create_user.__doc__ == "Create one."


def test_transaction_retries_and_asks_again_every_attempt() -> None:
    attempts: list[Database] = []
    first, second = _database(), _database()

    def source() -> Database:
        return first if not attempts else second

    retrying = transaction(using=source, retry_on=ValueError, backoff=lambda _: 0)

    @retrying
    def create(name: str) -> None:
        database = source()
        attempts.append(database)
        database.session.add(User(name=name))
        if len(attempts) == 1:
            raise ValueError

    create("ada")

    assert isinstance(retrying, RetryingTransaction)
    assert attempts == [first, second]
    assert _names(first) == []
    assert _names(second) == ["ada"]
    first.dispose()
    second.dispose()


# autocommit


def test_autocommit_finds_the_database_when_it_is_called(
    real: Databases, test_db: Database
) -> None:
    @autocommit
    def isolation() -> Any:
        return real.connection.get_execution_options().get("isolation_level")

    @autocommit(using=lambda: test_db)
    def create(name: str) -> None:
        test_db.connection.execute(
            sa.text("INSERT INTO users (name) VALUES (:name)"), {"name": name}
        )

    with real.override(test_db):
        assert isolation() == "AUTOCOMMIT"
    create("ada")

    assert _names(test_db) == ["ada"]


@pytest.mark.parametrize(
    "decorate",
    [transaction(using="nowhere"), autocommit(using="nowhere")],
    ids=["transaction", "autocommit"],
)
def test_an_alias_nobody_configured_fails_the_call_not_the_import(
    real: Databases,
    decorate: Callable[[Callable[[], None]], Callable[[], None]],
) -> None:
    def work() -> None:  # pragma: no cover - never reached
        pass

    decorated = decorate(work)

    with pytest.raises(UnknownDatabaseError):
        decorated()


# the registry's own blocks


@pytest.mark.parametrize(
    "block",
    ["transaction", "autocommit", "connect", "session_factory"],
)
def test_a_block_of_the_registry_decorated_before_follows(
    real: Databases, test_db: Database, block: str
) -> None:
    @getattr(real, block)()
    def create(name: str) -> None:
        real.session.add(User(name=name))
        real.session.commit()

    with real.override(test_db):
        create("ada")

    assert _names(test_db) == ["ada"]
    assert _names(real) == []


def test_a_bare_decorator_of_the_registry_follows(
    real: Databases, test_db: Database
) -> None:
    @real.transaction
    def create(name: str) -> None:
        _add(name)

    with real.override(test_db):
        create("ada")

    assert _names(test_db) == ["ada"]


def test_a_registered_default_is_found_when_the_block_opens(
    test_db: Database,
) -> None:
    registry = Databases()
    with _database() as registered:
        registry.register(DEFAULT_ALIAS, registered)

        @registry.transaction
        def create(name: str) -> None:
            registry.session.add(User(name=name))

        with registry.override(test_db):
            create("ada")
        create("grace")

        assert _names(test_db) == ["ada"]
        assert _names(registered) == ["grace"]
