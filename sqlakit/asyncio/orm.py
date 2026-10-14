from __future__ import annotations

from contextlib import asynccontextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Literal,
    Self,
    TypeVar,
    cast,
    overload,
)

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

from sqlakit._base import DEFAULT_ALIAS
from sqlakit._model import (
    BaseModel,
    BaseSoftDeletes,
    DatabaseDescriptor,
    every_attribute,
    names_of,
    related_to,
    soft_delete_column,
    tables_for,
)
from sqlakit._query import (
    BaseQuery,
    CursorPage,
    Page,
    key_names,
    keyed,
    merged,
    one_row,
    one_row_or_none,
    orderable,
    ordered,
)
from sqlakit.exceptions import InstanceNotFoundError

from ._db import Database
from ._registry import db

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Iterable,
        Mapping,
        Sequence,
    )

    from sqlalchemy.engine import CursorResult, Result, ScalarResult
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql import Executable
    from sqlalchemy.sql._typing import (
        _ColumnExpressionArgument,
        _TypedColumnClauseArgument,
    )
    from sqlalchemy.sql.selectable import ForUpdateParameter

__all__ = ["Model", "ModelMixin", "Query", "SoftDeletes"]

ModelT = TypeVar("ModelT")
RowT = TypeVar("RowT")
RowT_co = TypeVar("RowT_co", covariant=True)
QueryT = TypeVar("QueryT", bound="Query[Any]")
C0 = TypeVar("C0")
C1 = TypeVar("C1")
C2 = TypeVar("C2")


class Query(BaseQuery[ModelT]):
    """A query on the session of the block it runs in.

    Reached as `Model.query`. Every builder method returns a new query, so one
    can be kept and branched from:

    ```python
    active = User.query.where(User.is_active)
    await active.count()
    await active.order_by(User.id).all()
    ```
    """

    @classmethod
    def as_descriptor(cls) -> Self:
        """Return this query as the ``query`` attribute of a model.

        ```python
        class UserQuery(Query["User"]):
            def active(self) -> Self:
                return self.where(User.is_active.is_(True))


        class User(Model):
            query = UserQuery.as_descriptor()
        ```

        A type checker reads `User.query` as `UserQuery`, so the methods you added
        are visible on it.
        """
        return cast("Self", QueryDescriptor(cls))

    def __init__(self, model: type[ModelT], db: Database) -> None:
        super().__init__(model, db)

    async def _rows(self, statement: Executable) -> ScalarResult[ModelT]:
        """Rows as entities, one per identity.

        A `joinedload` against a collection repeats the parent row once per
        child, and SQLAlchemy asks to be told what to do about it.
        """
        result = await self.db.session.scalars(statement)
        return result.unique()

    async def get(self, ident: Any) -> ModelT | None:  # noqa: ANN401
        """Look the row up by primary key, or return None.

        Goes through the session's identity map, so a row already loaded costs no
        query. This query's loader options and lock carry over, nothing else does,
        and a query narrowed by `where` is refused rather than ignored. Loader
        options read the row again even when the session holds it.
        """
        options = self._lookup_options()
        statement = self._lookup_statement(ident)
        if statement is not None:
            statement = statement.options(*options)
            if options:
                # Loader options say nothing to a row the session already
                # holds, unless it is told to read that row again.
                statement = statement.execution_options(populate_existing=True)
            result = await self._rows(statement)
            return result.one_or_none()
        return await self.db.session.get(
            self.model,
            ident,
            options=options,
            with_for_update=self._lock(),
            populate_existing=bool(options),
        )

    async def get_one(self, ident: Any) -> ModelT:  # noqa: ANN401
        """Look the row up by primary key.

        Raises:
            InstanceNotFoundError: if there is no such row.

        """
        instance = await self.get(ident)
        if instance is None:
            raise InstanceNotFoundError(self.model.__name__)
        return instance

    async def all(self) -> Sequence[ModelT]:
        """Return every matching row."""
        result = await self._rows(self._executable())
        return result.all()

    @overload
    async def in_bulk(self) -> dict[Any, ModelT]: ...

    @overload
    async def in_bulk(
        self, key: _TypedColumnClauseArgument[C0], /
    ) -> dict[C0, ModelT]: ...

    @overload
    async def in_bulk(
        self,
        key: _TypedColumnClauseArgument[C0],
        other: _TypedColumnClauseArgument[C1],
        /,
    ) -> dict[tuple[C0, C1], ModelT]: ...

    @overload
    async def in_bulk(
        self,
        key: _TypedColumnClauseArgument[C0],
        other: _TypedColumnClauseArgument[C1],
        third: _TypedColumnClauseArgument[C2],
        /,
    ) -> dict[tuple[C0, C1, C2], ModelT]: ...

    @overload
    async def in_bulk(
        self, *keys: _TypedColumnClauseArgument[Any]
    ) -> dict[Any, ModelT]: ...

    async def in_bulk(
        self, *keys: _TypedColumnClauseArgument[Any]
    ) -> dict[Any, ModelT]:
        """Return every matching row, under the value it has in these columns.

        ```python
        User.query.where(User.id.in_(ids)).in_bulk()  # dict[int, User], by key
        User.query.in_bulk(User.email)  # dict[str, User]
        Seat.query.in_bulk(Seat.row, Seat.col)  # dict[tuple[int, str], Seat]
        ```

        One column keys by its value and several by the tuple of theirs, so
        the checker knows the key's type. The rows keep the query's order, and
        a key column the query defers is loaded with the row.

        Raises:
            DuplicateKeyError: if two rows share a key.
            UnknownFieldError: if a key is not a column of the model.

        """
        # A key the model defers is read with the row rather than one row at a time.
        names = key_names(self.model, keys)
        rows = await self.undefer(*keys).all()
        return keyed(rows, self.model, names)

    async def first(self) -> ModelT | None:
        """Return the first matching row, or None."""
        # A raw statement takes no limit; the first row is read off the result.
        query = self if self._statement is not None else self.limit(1)
        result = await self._rows(query._executable())  # noqa: SLF001
        return result.first()

    async def latest(self, column: Any) -> ModelT | None:  # noqa: ANN401
        """Return the row with the greatest value in this column, or None.

        Takes a column, or the name of a field the model offers.
        """
        return await self.order_by(self._directed(column, descending=True)).first()

    async def earliest(self, column: Any) -> ModelT | None:  # noqa: ANN401
        """Return the row with the least value in this column, or None.

        Takes a column, or the name of a field the model offers.
        """
        return await self.order_by(self._directed(column, descending=False)).first()

    async def one(self) -> ModelT:
        """Return the single matching row.

        Raises:
            InstanceNotFoundError: if there is none.
            MultipleInstancesFoundError: if there is more than one.

        """
        result = await self._rows(self._executable())
        return one_row(result, self.model.__name__)

    async def one_or_none(self) -> ModelT | None:
        """Return the single matching row, or None.

        Raises:
            MultipleInstancesFoundError: if there is more than one.

        """
        result = await self._rows(self._executable())
        return one_row_or_none(result, self.model.__name__)

    async def count(self) -> int:
        """Count the matching rows."""
        return await self.db.session.scalar(self._count_statement()) or 0

    async def exists(self) -> bool:
        """Check whether any row matches."""
        return bool(await self.db.session.scalar(self._exists_statement()))

    @overload
    async def page(
        self, *, limit: int, offset: int = 0, total: Literal[True] = True
    ) -> Page[ModelT]: ...

    @overload
    async def page(
        self, *, limit: int, offset: int = 0, total: Literal[False]
    ) -> Page[ModelT, None]: ...

    async def page(
        self, *, limit: int, offset: int = 0, total: bool = True
    ) -> Page[ModelT] | Page[ModelT, None]:
        """Read one page of rows, with the total.

        The model's key is appended to the ordering, so a row that ties with another
        keeps its place between requests.

        The total costs a second query over the whole match, and the database walks
        the rows the offset skips; past the first few pages
        [`cursor_page`][sqlakit.asyncio.orm.Query.cursor_page] does
        neither. With ``total=False`` there is no counting query: the page reads one
        row more than it shows, which answers `Page.has_next` and leaves
        `Page.total` None.

        Raises:
            UnorderedPageError: if the query names no order.

        """
        if not total:
            result = await self._rows(
                self._page_statement(limit=limit + 1, offset=offset)
            )
            rows = list(result.all())
            return Page(
                items=rows[:limit],
                total=None,
                limit=limit,
                offset=offset,
                has_next=len(rows) > limit,
            )
        statement = self._page_statement(limit=limit, offset=offset)
        counted = await self.count()
        if counted <= offset:
            return Page(items=[], total=counted, limit=limit, offset=offset)
        result = await self._rows(statement)
        return Page(items=result.all(), total=counted, limit=limit, offset=offset)

    async def cursor_page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> CursorPage[ModelT]:
        """Read one page of rows, and the cursors that read the ones either side.

        The page follows the order the query carries, with the model's key appended
        so that rows sharing a value cannot fall on both sides of a boundary. Rows
        inserted in between do not shift it, and a page deep in the table costs the
        same as the first.

        Either cursor of a page goes back in as ``cursor``, and the page it names
        comes out: the direction rides along in the cursor.

        ```python
        page = await User.query.order_by("created_at.desc").cursor_page(limit=20)
        older = await User.query.order_by("created_at.desc").cursor_page(
            limit=20, cursor=page.next_cursor
        )
        ```

        Raises:
            InvalidCursorError: if the cursor was not made for this ordering.

        """
        statement = self._cursor_statement(limit=limit, cursor=cursor)
        result = await self._rows(statement)
        return self._cursor_page(list(result.all()), limit=limit, cursor=cursor)

    async def chunks(self, size: int) -> AsyncIterator[Sequence[ModelT]]:
        """Read every matching row, ``size`` of them at a time.

        One statement, whose rows are fetched in batches rather than all at once,
        for the job that walks a table too large to hold:

        ```python
        async with db.transaction():
            async for contacts in Contact.query.where(Contact.is_stale).chunks(1000):
                await deliver(contacts)
        ```

        The rows come off a cursor the database holds open, so the walk is one
        transaction, and committing part-way through ends it. To commit each batch,
        page through the table with `cursor_page` instead.

        A batch is whole rows, which a `joinedload` of a collection is not: load
        collections with `selectinload` here.
        """
        result = await self.db.session.stream_scalars(
            self._executable(), execution_options={"yield_per": size}
        )
        async for batch in result.partitions(size):
            yield batch

    @overload
    def only_columns(
        self, column: _TypedColumnClauseArgument[C0], /
    ) -> ColumnQuery[C0]: ...

    @overload
    def only_columns(
        self,
        column: _TypedColumnClauseArgument[C0],
        other: _TypedColumnClauseArgument[C1],
        /,
    ) -> ColumnQuery[tuple[C0, C1]]: ...

    @overload
    def only_columns(
        self,
        column: _TypedColumnClauseArgument[C0],
        other: _TypedColumnClauseArgument[C1],
        third: _TypedColumnClauseArgument[C2],
        /,
    ) -> ColumnQuery[tuple[C0, C1, C2]]: ...

    @overload
    def only_columns(
        self, *columns: _TypedColumnClauseArgument[Any]
    ) -> ColumnQuery[Any]: ...

    def only_columns(
        self, *columns: _TypedColumnClauseArgument[Any]
    ) -> ColumnQuery[Any]:
        """Read these columns instead of whole rows.

        ```python
        names = User.query.where(User.is_active).only_columns(User.name).all()
        ```
        """
        return ColumnQuery(
            self.model,
            self._columns_select(columns),
            self.db,
            scalar=len(columns) == 1,
        )

    async def create(
        self,
        values: Mapping[str, Any] | None = None,
        /,
        **fields: Any,  # noqa: ANN401
    ) -> ModelT:
        """Write a new row, and return it as an instance.

        ```python
        user = await User.query.create(name="ada", team="red")
        user = await User.query.create(payload.model_dump())
        ```

        The fields are keywords, a mapping, or both, where a keyword replaces
        the value of that name.

        The row goes through the session, so defaults, relationships and the identity
        map behave as they do for a model that saves itself. What it adds is a write
        that needs no model layer: `Query(User, db).create(...)` works on any mapped
        class.
        """
        instance = self.model(**merged(values, fields))
        self.db.session.add(instance)
        await self._persist()
        return instance

    async def create_many(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Write these rows in one statement, and return how many.

        ```python
        await User.query.create_many([{"name": "ada"}, {"name": "grace"}])
        ```

        Nothing is instantiated and no ORM event fires, which is the point for a job
        that loads a file.
        """
        if not rows:
            return 0
        await self.db.session.execute(sa.insert(self.model), list(rows))
        await self._persist()
        return len(rows)

    async def update(
        self,
        values: Mapping[str, Any] | None = None,
        /,
        **fields: Any,  # noqa: ANN401
    ) -> int:
        """Write these values to every matching row, and return how many.

        A mapping, keywords, or both: `update({"team": "green"})` and
        `update(team="green")` write the same statement.

        One statement, so the session's objects are updated from the database
        rather than in memory. Only the narrowing carries over.

        Raises:
            BulkQueryError: if the query carries anything a statement drops.

        """
        result = await self.db.session.execute(
            self._update_statement(merged(values, fields))
        )
        await self._persist()
        return cast("CursorResult[Any]", result).rowcount

    async def delete(self, *, force: bool = False) -> int:
        """Delete every matching row, and return how many.

        A model that [soft-deletes](models.md#soft-deletes) is marked rather
        than removed. Pass ``force`` to remove the rows anyway.

        Raises:
            BulkQueryError: if the query carries anything a statement drops.

        """
        result = await self.db.session.execute(self._delete_statement(force=force))
        await self._persist()
        return cast("CursorResult[Any]", result).rowcount

    async def _persist(self) -> None:
        """Commit what a write left behind, unless a block owns the commit.

        Inside a transaction the statement is part of it and the block decides.
        Outside one there is nobody to commit, and a write that reported rows
        would be rolled back when the connection is returned.
        """
        if not self.db.in_transaction():
            await self.db.session.commit()


class ColumnQuery(Generic[RowT]):
    """Rows of the columns asked for, not of the model.

    Built by `Query.only_columns`. One column comes back as its own values,
    several come back as tuples.
    """

    def __init__(
        self,
        model: type[Any],
        select: sa.Select[Any],
        db: Database,
        *,
        scalar: bool,
    ) -> None:
        self.model = model
        self._select = select
        self.db = db
        self.scalar = scalar

    def where(self, *criteria: _ColumnExpressionArgument[bool]) -> Self:
        """Narrow the rows."""
        return self.with_select(self._select.where(*criteria))

    def order_by(
        self,
        *criteria: Any,  # noqa: ANN401
        ignore_case: bool | Sequence[str] = False,
        nulls: Literal["first", "last"] | None = None,
    ) -> Self:
        """Order the rows, by columns or by the names the model offers."""
        return self.with_select(
            ordered(
                self._select,
                orderable(self.model),
                criteria,
                ignore_case=ignore_case,
                nulls=nulls,
                model=self.model,
            )
        )

    def distinct(self) -> Self:
        """Drop the duplicate rows."""
        return self.with_select(self._select.distinct())

    def limit(self, limit: int) -> Self:
        """Take at most this many rows."""
        return self.with_select(self._select.limit(limit))

    def offset(self, offset: int) -> Self:
        """Skip this many rows."""
        return self.with_select(self._select.offset(offset))

    def with_select(self, select: sa.Select[Any]) -> Self:
        return type(self)(self.model, select, self.db, scalar=self.scalar)

    async def _rows(self) -> ScalarResult[Any] | Result[Any]:
        if self.scalar:
            return await self.db.session.scalars(self._select)
        return await self.db.session.execute(self._select)

    async def all(self) -> Sequence[RowT]:
        """Return every matching row."""
        rows = await self._rows()
        return cast("Sequence[RowT]", rows.all())

    async def first(self) -> RowT | None:
        """Return the first matching row, or None."""
        rows = await self._rows()
        return cast("RowT | None", rows.first())

    async def one(self) -> RowT:
        """Return the single matching row.

        Raises:
            InstanceNotFoundError: if there is none.
            MultipleInstancesFoundError: if there is more than one.

        """
        rows = await self._rows()
        return cast("RowT", one_row(rows, self.model.__name__))

    async def one_or_none(self) -> RowT | None:
        """Return the single matching row, or None.

        Raises:
            MultipleInstancesFoundError: if there is more than one.

        """
        rows = await self._rows()
        return cast("RowT | None", one_row_or_none(rows, self.model.__name__))


class QueryDescriptor(Generic[QueryT]):
    """Hand out a query bound to the model's database, as `Model.query`.

    Assign one to give a model, or a whole base, a query of your own:

    ```python
    class Base(Model):
        __abstract__ = True

        query = QueryDescriptor(AppQuery)
    ```

    Given `Query` itself, as `ModelMixin` does, it reads as a query of the model
    it is read from: `User.query` is a `Query[User]`, so `User.query.get(1)` is
    a `User | None`.
    """

    def __init__(self, query_class: type[QueryT]) -> None:
        self.query_class = query_class

    # `QueryDescriptor(Query)` reads as a query of the model it is read from,
    # any other query class as itself.
    @overload
    def __get__(
        self: QueryDescriptor[Query[Any]],
        instance: object | None,
        owner: type[ModelT],
    ) -> Query[ModelT]: ...

    @overload
    def __get__(self, instance: object | None, owner: type[Any]) -> QueryT: ...

    def __get__(self, instance: object | None, owner: type[Any]) -> Query[Any]:
        return self.query_class(owner, owner.db)


class ModelMixin(BaseModel[Database]):
    """Adds saving, lookup and the current database to a declarative base.

    Mix it into your own base when it carries settings of its own, such as a
    ``type_annotation_map``, a naming convention or ``MappedAsDataclass``:

    ```python
    class Model(ModelMixin, MappedAsDataclass, DeclarativeBase):
        type_annotation_map = {str: sa.Text}
    ```

    `Model` is the same thing with a plain declarative base mixed in. The
    database is the one ``__db__`` names, the ``"default"`` alias of the
    importable registry, or one set with
    [`set_db`][sqlakit.asyncio.orm.ModelMixin.set_db].
    """

    __db__ = DEFAULT_ALIAS
    __dbs__ = db

    # Not annotated: a model's `query = UserQuery.as_descriptor()` would take
    # the declared type and read as `Query[User]`.
    query = QueryDescriptor(Query)

    db: ClassVar[DatabaseDescriptor[Database]] = DatabaseDescriptor()

    @classmethod
    @asynccontextmanager
    async def provisioned_tables(cls, alias: str | None = None) -> AsyncGenerator[None]:
        """Create the tables that belong on this model's database, and drop them after.

        A test session opens this once, around everything that needs a schema:

        ```python
        @pytest.fixture(scope="session")
        async def tables():
            async with Model.provisioned_tables():
                yield
        ```

        With models on more than one database, name the alias, and each database
        gets the tables of the models pointed at it.
        """
        db = cls.db if alias is None else cls.dbs[alias]
        async with db.provisioned_tables(cls.metadata, tables=tables_for(cls, db)):
            yield

    async def save(self) -> Self:
        """Write this instance out.

        Commits when nothing else owns a transaction, and flushes when one
        does, so what was written is there for the queries that follow.

        Raises:
            DetachedInstanceError: if its session has closed.

        """
        self._prepare_save()
        await self._persist()
        return self

    async def delete(self, *, force: bool = False) -> None:
        """Delete the row for this instance.

        A model that [soft-deletes](models.md#soft-deletes) is marked rather
        than removed. Pass ``force`` to remove the row anyway.
        """
        column = soft_delete_column(type(self))
        if column is None or force:
            await self.db.session.delete(self)
        else:
            setattr(self, column, sa.func.now())
        await self._persist()

    async def merge(self) -> Self:
        """Copy this instance into the current session and return the copy.

        An instance loaded in a block that has since closed needs this before it can
        be saved in another one. The row is read again, so anything changed in
        between is overwritten by what this instance holds.
        """
        return await self.db.session.merge(self)

    async def refresh(
        self,
        *attributes: str | InstrumentedAttribute[Any],
        attribute_names: Iterable[str] | None = None,
        with_relationships: bool = False,
        with_for_update: ForUpdateParameter = None,
    ) -> None:
        """Read this instance back from the database.

        ```python
        await user.refresh()  # every column, and the relationships already loaded
        await user.refresh(User.team)  # and one that was not, though it raises on load
        await user.refresh(with_relationships=True)  # every relationship the model has
        ```

        Args:
            attributes: The attributes to read again, rather than all of them, as
                the model declares them or by name. A relationship named here is
                loaded, which is how a `lazy="raise"` one is read after a refresh.
            attribute_names: The same, for names a caller holds as a list.
            with_relationships: Read every relationship the model declares, loaded
                or not, which costs a statement each. For a test that compares the
                whole instance and would otherwise name them one by one.
            with_for_update: Lock the row while it is read, as
                ``Session.refresh`` takes it: `True` for a plain ``FOR UPDATE``,
                or a mapping such as ``{"read": True}``.

        """
        names = names_of(attributes, attribute_names)
        if with_relationships:
            names = list(dict.fromkeys([*(names or ()), *every_attribute(type(self))]))
        await self.db.session.refresh(
            self,
            attribute_names=names,
            with_for_update=with_for_update,
        )
        if with_relationships:
            # The relationships hand back the instances the session holds, and
            # those carry the values they were loaded with.
            for related in related_to(self):
                self.db.session.expire(related)

    async def _persist(self) -> None:
        db = self.db
        if db.in_transaction():
            await db.session.flush()
        else:
            await db.session.commit()


class Model(ModelMixin, DeclarativeBase):
    """A declarative base whose instances know how to save themselves.

    ```python
    class User(Model):
        __tablename__ = "users"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]


    await User(name="ada").save()
    ```
    """

    __abstract__ = True


class SoftDeletes(BaseSoftDeletes):
    """Rows this model marks as deleted instead of removing.

    ```python
    class Note(Model, SoftDeletes):
        __tablename__ = "notes"


    await note.delete()  # UPDATE notes SET deleted_at = now()
    await note.restore()  # and back
    ```

    Reads skip the marked rows, `get()` included. `Note.query.with_deleted()`
    reads them, and `delete(force=True)` removes them for good.
    """

    async def restore(self: Any) -> Any:  # noqa: ANN401 - a mixin, on any model
        """Clear the mark, and save the row."""
        setattr(self, self.__soft_delete__, None)
        return await self.save()
