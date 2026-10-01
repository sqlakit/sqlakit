"""A database found when the code runs, awaited."""

import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import sqlakit
from sqlakit import DEFAULT_ALIAS
from sqlakit.asyncio import (
    Database,
    Databases,
    RetryingTransaction,
    autocommit,
    db,
    transaction,
)
from sqlakit.asyncio.orm import ModelMixin


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _database() -> Database:
    database = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})
    async with database.transaction() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return database


async def _names(database: Database) -> list[str]:
    async with database.connect() as conn:
        rows = await conn.scalars(sa.text("SELECT name FROM users ORDER BY id"))
        return list(rows)


@pytest.fixture
async def real() -> AsyncIterator[Databases]:
    db.configure(
        {
            "default": {
                "url": "sqlite+aiosqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
            "replica": {
                "url": "sqlite+aiosqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
        }
    )
    for alias in db.aliases:
        async with db[alias].transaction() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield db
    await db.dispose()


@pytest.fixture
async def test_db() -> AsyncIterator[Database]:
    async with await _database() as database:
        yield database


@pytest.mark.anyio
async def test_the_default_database_is_another_for_the_block(
    real: Databases, test_db: Database
) -> None:
    with real.override(test_db) as overridden:
        assert overridden is test_db
        assert User.db is test_db
        async with real.transaction():
            await User(name="ada").save()

    assert await _names(test_db) == ["ada"]
    assert await _names(real) == []
    assert real[DEFAULT_ALIAS] is real


@pytest.mark.anyio
async def test_transaction_follows_an_override(
    real: Databases, test_db: Database
) -> None:
    @transaction
    async def create(name: str) -> str:
        db.session.add(User(name=name))
        return name

    with real.override(test_db):
        assert await create("ada") == "ada"
    await create("grace")

    assert await _names(test_db) == ["ada"]
    assert await _names(real) == ["grace"]


@pytest.mark.anyio
async def test_transaction_takes_an_alias_and_a_callable(
    real: Databases, test_db: Database
) -> None:
    @transaction(using="replica")
    async def on_the_replica(name: str) -> None:
        real["replica"].session.add(User(name=name))

    @transaction(using=lambda: test_db)
    async def on_the_test_db(name: str) -> None:
        test_db.session.add(User(name=name))

    await on_the_replica("ada")
    await on_the_test_db("grace")

    assert await _names(real["replica"]) == ["ada"]
    assert await _names(test_db) == ["grace"]


@pytest.mark.anyio
async def test_transaction_rolls_back_what_raised(test_db: Database) -> None:
    @transaction(using=test_db)
    async def create(name: str) -> None:
        test_db.session.add(User(name=name))
        await test_db.session.flush()
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        await create("ada")

    assert await _names(test_db) == []


@pytest.mark.anyio
async def test_transaction_retries_and_asks_again_every_attempt() -> None:
    attempts: list[Database] = []
    first, second = await _database(), await _database()

    def source() -> Database:
        return first if not attempts else second

    retrying = transaction(using=source, retry_on=ValueError, backoff=lambda _: 0)

    @retrying
    async def create(name: str) -> None:
        database = source()
        attempts.append(database)
        database.session.add(User(name=name))
        if len(attempts) == 1:
            raise ValueError

    await create("ada")

    assert isinstance(retrying, RetryingTransaction)
    assert attempts == [first, second]
    assert await _names(first) == []
    assert await _names(second) == ["ada"]
    await first.dispose()
    await second.dispose()


@pytest.mark.anyio
async def test_autocommit_finds_the_database_when_it_is_called(
    real: Databases, test_db: Database
) -> None:
    @autocommit
    async def isolation() -> Any:
        options = real.connection.sync_connection.get_execution_options()  # ty: ignore[unresolved-attribute]
        return options.get("isolation_level")

    @autocommit(using=lambda: test_db)
    async def create(name: str) -> None:
        await test_db.connection.execute(
            sa.text("INSERT INTO users (name) VALUES (:name)"), {"name": name}
        )

    with real.override(test_db):
        assert await isolation() == "AUTOCOMMIT"
    await create("ada")

    assert await _names(test_db) == ["ada"]


@pytest.mark.parametrize(
    ("sync", "awaited"),
    [(sqlakit.transaction, transaction), (sqlakit.autocommit, autocommit)],
    ids=["transaction", "autocommit"],
)
@pytest.mark.anyio
async def test_the_decorators_take_what_the_synchronous_ones_take(
    sync: Callable[..., Any], awaited: Callable[..., Any]
) -> None:
    def parameters(decorator: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
        return [
            (parameter.name, parameter.kind, parameter.default)
            for parameter in inspect.signature(decorator).parameters.values()
        ]

    assert parameters(awaited) == parameters(sync)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "block",
    ["transaction", "autocommit", "connect", "session_factory"],
)
async def test_a_block_of_the_registry_decorated_before_follows(
    real: Databases, test_db: Database, block: str
) -> None:
    @getattr(real, block)()
    async def create(name: str) -> None:
        real.session.add(User(name=name))
        await real.session.commit()

    with real.override(test_db):
        await create("ada")

    assert await _names(test_db) == ["ada"]
    assert await _names(real) == []
