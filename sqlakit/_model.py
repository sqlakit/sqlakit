from __future__ import annotations

# Imported here rather than under TYPE_CHECKING: SQLAlchemy resolves the
# annotation of `deleted_at` in this module, and needs both names at runtime.
from collections.abc import Iterable  # noqa: TC003
from datetime import datetime  # noqa: TC003
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Protocol,
    Self,
    TypeVar,
    cast,
)

import sqlalchemy as sa
import sqlalchemy.orm
from sqlalchemy.orm import Mapped  # noqa: TC002
from sqlalchemy.orm.attributes import set_committed_value

from .exceptions import (
    DEFAULT_ALIAS,
    DetachedInstanceError,
    MissingRegistryError,
    SQLAKitError,
    UnknownFieldError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._base import BaseDatabase


__all__ = [
    "BaseModel",
    "BaseSoftDeletes",
    "DatabaseDescriptor",
    "DatabaseRegistry",
    "DatabaseSource",
    "RegistryDescriptor",
    "db_for",
    "resolve_alias",
    "soft_delete_column",
    "tables_for",
]

DatabaseT = TypeVar("DatabaseT", bound="BaseDatabase[Any, Any]")


class DatabaseSource(Protocol):
    """The registry an alias is looked up in: the importable one, or a stand-in."""

    def __getitem__(self, alias: str, /) -> BaseDatabase[Any, Any]: ...


class DatabaseDescriptor(Generic[DatabaseT]):
    """Resolves ``__db__``, on the class as well as on an instance."""

    def __get__(
        self,
        instance: object | None,
        owner: type[BaseModel[DatabaseT]],
    ) -> DatabaseT:
        # `ClassVar` cannot hold a type variable, so the class declares what
        # every model has and the subclass narrows it to its own database.
        return cast("DatabaseT", db_for(owner))


class DatabaseRegistry(DatabaseSource, Protocol):
    """A source that also takes a database under an alias it does not have."""

    def register(self, alias: str, db: Any) -> None: ...  # noqa: ANN401


class RegistryDescriptor:
    """Reads ``__dbs__``, on the class as well as on an instance."""

    def __get__(self, instance: object | None, owner: type[Any]) -> Any:  # noqa: ANN401
        return owner.__dbs__


class BaseModel(Generic[DatabaseT]):
    """The sync and async models share this: everything that is not IO.

    A model works on the database named by ``__db__``: an alias in the
    importable registry, or a database of its own.
    """

    __db__: ClassVar[str | BaseDatabase[Any, Any]] = DEFAULT_ALIAS
    """The database this model works on: an alias, or one of its own."""

    __dbs__: ClassVar[DatabaseSource]
    """Where an alias in ``__db__`` is looked up."""

    # Declared, not assigned: the declarative base a model is built on brings
    # them, and saying so here lets the helpers below read them.
    registry: ClassVar[sa.orm.registry]
    metadata: ClassVar[sa.MetaData]

    # Unannotated on purpose: an annotation here reads as a field to
    # pydantic, and SQLModel models would refuse to build.
    db = DatabaseDescriptor[DatabaseT]()
    dbs = RegistryDescriptor()

    @classmethod
    def set_db(cls, db: str | DatabaseT) -> None:
        """Point this model, and the ones under it, at a database.

        Takes an alias in the importable registry, or a database of its own for an
        application that never configures that one. Set it on your own base rather
        than on the one this library ships:

        ```python
        Base.set_db(Database(DB_URL))
        Base.set_db("replica")
        ```
        """
        cls.__db__ = db

    @classmethod
    def register_db(cls, db: DatabaseT, *, alias: str | None = None) -> None:
        """Give this model a database under an alias, and the ones under it too.

        The registry it goes in belongs to this class, so nothing global is
        configured and two sets of models can each have their own `shard`:

        ```python
        Base.register_db(Database(DB1_URL), alias="db1")
        Base.register_db(Database(DB2_URL), alias="db2")

        with Base.dbs.using("db2").transaction():
            User(name="ada").save()
        ```

        A model still resolves through `__db__`, the routers and the open
        `using()` block, in that order. A model left on the default alias
        follows `using()`, and that makes the switch above work.

        Without an alias it goes in as the default one, which is where a model
        that names no database lives:

        ```python
        Base.register_db(Database(DB_URL))
        ```

        The registry holds it either way, so `dbs["default"]`,
        `dbs.transactions()` and `using()` all reach what was registered.
        [`set_db`][sqlakit.orm.ModelMixin.set_db] is the other way to name a
        database, and it leaves the registry out of it.

        Raises:
            AliasInUseError: if another database holds that alias.
            DefaultAliasError: if the registry already has a default database.

        """
        if _owns_no_registry(cls):
            # A registry of its own: registering into the importable one would
            # configure it for every model in the process. A class under one
            # that already has its own registers into that one.
            cls.__dbs__ = type(cls.__dbs__)()
        cast("DatabaseRegistry", cls.__dbs__).register(alias or DEFAULT_ALIAS, db)

    def update(self, values: Mapping[str, Any]) -> Self:
        """Set these fields on this instance, and return it.

        For a request that carries only the fields it means to change:

        ```python
        user.update(payload.model_dump(exclude_unset=True)).save()
        ```

        Every key has to be a field of the model, so a typo is an error rather than
        an attribute nobody reads. A `None` is a value like any other: it is how a
        request clears a nullable field.

        Raises:
            UnknownFieldError: if a key is not a field of this model.

        """
        fields = sa.inspect(type(self), raiseerr=True).attrs
        for name, value in values.items():
            if name not in fields:
                raise UnknownFieldError(type(self).__name__, name)
            setattr(self, name, value)
        return self

    def set_loaded(self, name: str, value: Any) -> Self:  # noqa: ANN401
        """Give a relationship a value the database is not asked for, and return this.

        The value is taken as one that was loaded, so reading it costs nothing:

        ```python
        campaign.set_loaded("esp", esp)  # the one this code just used
        campaign.set_loaded("thumbnail", None)  # known to be empty
        ```

        A `lazy="raise"` relationship needs this when the value is in hand:
        rows fetched for a whole page at once, a row this block created, or an
        instance that outlives the session that loaded it.

        It says what the database holds rather than changing it: the value is not
        written on save, does not mark the instance dirty, and does not reach the
        other side of the relationship. Say something untrue and the instance says it
        too. `refresh(attribute_names=["esp"])` is the other way to fill a
        relationship nobody loaded: right by construction, one query, session still
        open.

        Raises:
            UnknownFieldError: if the model has no such field.

        """
        if name not in sa.inspect(type(self), raiseerr=True).attrs:
            raise UnknownFieldError(type(self).__name__, name)
        set_committed_value(self, name, value)
        return self

    @property
    def is_persisted(self) -> bool:
        """Whether a row exists, or existed, for this instance."""
        return sa.inspect(self, raiseerr=True).has_identity

    @property
    def is_modified(self) -> bool:
        """Whether any attribute changed since the last flush."""
        return bool(self.modified_fields)

    @property
    def was_deleted(self) -> bool:
        """Whether this instance was deleted, even once it is detached."""
        return sa.inspect(self, raiseerr=True).was_deleted

    @property
    def modified_fields(self) -> set[str]:
        """The attributes changed since the last flush."""
        state = sa.inspect(self, raiseerr=True)
        return set(state.mapper.attrs.keys()) - state.unmodified

    def _prepare_save(self) -> None:
        """Put this instance in the session, or say why it cannot go there."""
        state = sa.inspect(self, raiseerr=True)
        if state.detached:
            raise DetachedInstanceError(type(self).__name__)
        if state.transient:
            self.db.session.add(self)


def _owns_no_registry(model: type[Any]) -> bool:
    """Whether this model still looks its aliases up in the importable registry."""
    declared = next(
        (klass for klass in model.__mro__ if "__dbs__" in klass.__dict__), None
    )
    return (
        declared is None or declared.__module__.split(".")[0] == __name__.split(".")[0]
    )


def db_for(model: type[Any]) -> BaseDatabase[Any, Any]:
    """Return the database a model lives on.

    ``__db__`` says it, unless the source it names knows better: a registry
    asks the block's `using()` and the routers first.
    """
    source = getattr(model, "__dbs__", None)
    resolve = getattr(source, "db_for", None)
    if resolve is not None:
        return cast("BaseDatabase[Any, Any]", resolve(model))
    placement = model.__db__
    if isinstance(placement, str):
        if source is None:
            raise MissingRegistryError(model.__name__, placement)
        return source[placement]
    return placement


def names_of(
    attributes: tuple[Any, ...], listed: Iterable[str] | None
) -> list[str] | None:
    """Return the attributes to reload, as the session names them.

    A rename and an editor follow the attribute a model declares, and neither
    follows a string, so a caller may name either.
    """
    named = [
        attribute if isinstance(attribute, str) else attribute.key
        for attribute in attributes
    ]
    named += list(listed or ())
    return named or None


def every_attribute(model: type[Any]) -> list[str]:
    """Return every attribute a model declares, relationships included."""
    return list(sa.inspect(model).attrs.keys())


def related_to(instance: Any) -> list[Any]:  # noqa: ANN401 - a model of either API
    """Return the instances the loaded relationships of this one hold.

    A relationship read again hands back the instances the session already
    holds, with the values they were loaded with, so a caller that wants the
    rows as they are now expires them.
    """
    state = sa.inspect(instance)
    found = []
    for name in state.mapper.relationships.keys():  # noqa: SIM118
        if name in state.unloaded:
            continue
        value = state.dict.get(name)
        if value is None:
            continue
        found.extend(value if isinstance(value, (list, set, tuple)) else [value])
    return found


def resolve_alias(model: type[Any], alias: str) -> BaseDatabase[Any, Any]:
    """Return the database a model knows under that alias.

    Raises:
        MissingRegistryError: if the model looks aliases up nowhere.

    """
    source = getattr(model, "__dbs__", None)
    if source is None:
        raise MissingRegistryError(model.__name__, alias)
    return source[alias]


def tables_for(
    model: type[BaseModel[Any]],
    db: BaseDatabase[Any, Any],
) -> list[sa.Table] | None:
    """Return the tables of this model's metadata that live on that database.

    None when they all do, as `create_all` wants for the ordinary
    case of one database. Otherwise the tables of the models pointed at it,
    and the tables that only reference those. An association table belongs
    with the rows it joins.
    """
    ours: set[sa.TableClause] = set()
    theirs: set[sa.TableClause] = set()
    for mapper in model.registry.mappers:
        try:
            owner = getattr(mapper.class_, "db", None)
        except SQLAKitError:
            # A model pointed at a database this run does not configure is not
            # on this one, and its tables are not ours to create.
            owner = None
        where = ours if owner is db else theirs
        where.update(mapper.tables)

    if not theirs:
        return None

    for table in model.metadata.tables.values():
        if table in ours or table in theirs:
            continue
        referenced = {key.column.table for key in table.foreign_keys}
        if referenced and referenced <= ours:
            ours.add(table)
    return [table for table in sorted(ours, key=str) if isinstance(table, sa.Table)]


class BaseSoftDeletes:
    """Rows this model marks as deleted instead of removing.

    Mixed into a model, it adds a ``deleted_at`` column, hides the rows that
    carry one, and has `delete()` stamp that column rather than issue a `DELETE`.
    ``__soft_delete__`` names the column, so a model with one of its own can set
    that and skip the mixin.

    Hiding the marked rows is its own step, not a ``__query_filter__``. A model
    can carry both, and `with_deleted()` and `unfiltered()` then lift one each.
    """

    __soft_delete__: ClassVar[str] = "deleted_at"

    deleted_at: Mapped[datetime | None] = sa.orm.mapped_column(
        # `timestamptz` on PostgreSQL, and nothing to SQLite: a mark compared
        # across time zones has to carry one.
        sa.DateTime(timezone=True),
        default=None,
    )


def soft_delete_column(model: type[Any]) -> str | None:
    """Return the column a model marks deleted rows with, if it marks them."""
    return getattr(model, "__soft_delete__", None)
