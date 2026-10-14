"""A small FastAPI service on SQLAKit.

Run it with `uvicorn examples.fastapi_app:app`, or read it as the shape of an
application: one db, a transaction named on each endpoint, and the errors
a query raises answered in one place.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Mapped, mapped_column

from sqlakit import CursorPage, InstanceNotFoundError, Page, UnknownOrderFieldError
from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import Model

db = Database(os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///app.db"))


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- The models ---


class Base(Model):
    __abstract__ = True


Base.set_db(db)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
    team: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserCreate(BaseModel):
    name: str
    team: str = ""


class UserResponse(BaseModel, from_attributes=True):
    id: int
    name: str
    team: str


class UserPage(BaseModel, from_attributes=True):
    items: list[UserResponse]
    total: int
    has_next: bool


class UserFeed(BaseModel, from_attributes=True):
    items: list[UserResponse]
    next_cursor: str | None
    previous_cursor: str | None


# --- The application ---


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # A real service would run migrations; creating the tables keeps the example short.
    async with db.transaction() as conn:
        await conn.run_sync(Model.metadata.create_all)
    yield
    await db.dispose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(InstanceNotFoundError)
async def not_found(request: Request, error: InstanceNotFoundError) -> JSONResponse:
    return JSONResponse({"detail": f"{error.model} not found"}, status_code=404)


@app.exception_handler(UnknownOrderFieldError)
async def bad_sort(request: Request, error: UnknownOrderFieldError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=400)


# --- The endpoints ---


@app.post("/users", status_code=201, response_model=UserResponse)
@db.transaction
async def create_user(payload: UserCreate) -> User:
    return await User(name=payload.name, team=payload.team).save()


@app.get("/users/{user_id}", response_model=UserResponse)
@db.autocommit
async def get_user(user_id: int) -> User:
    return await User.query.get_one(user_id)


@app.get("/users", response_model=UserPage)
@db.autocommit
async def list_users(
    sort: str = "created_at.desc",
    limit: int = 20,
    offset: int = 0,
) -> Page[User]:
    return await User.query.order_by(sort).page(limit=limit, offset=offset)


@app.get("/feed", response_model=UserFeed)
@db.autocommit
async def feed(limit: int = 20, cursor: str | None = None) -> CursorPage[User]:
    return await User.query.order_by("created_at.desc").cursor_page(
        limit=limit, cursor=cursor
    )


@app.post("/teams/{team}/rename")
@db.transaction
async def rename_team(team: str, to: str) -> dict:
    moved = await User.query.where(User.team == team).update({"team": to})
    return {"moved": moved}


@app.get("/health")
async def health() -> dict:
    return {"database": await db.ping()}
