"""SQLAlchemy ORM models for PPBase system tables.

System tables have fixed schemas and are managed via Alembic migrations.
Dynamic collection tables are handled separately by the SchemaManager.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all system-table ORM models."""


# ---------------------------------------------------------------------------
# _collections
# ---------------------------------------------------------------------------


class CollectionRecord(Base):
    """Stores collection definitions (field schemas, rules, options)."""

    __tablename__ = "_collections"

    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        server_default="base",
    )
    system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    schema: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSONB,
        nullable=False,
        server_default="[]",
    )
    indexes: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSONB,
        nullable=False,
        server_default="[]",
    )
    list_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    update_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_collections_name"),
    )


# ---------------------------------------------------------------------------
# _superusers
# ---------------------------------------------------------------------------


class SuperuserRecord(Base):
    """Superuser / admin accounts.

    In PocketBase v0.23+ admins are stored in a special ``_superusers`` auth
    collection. PPBase now follows this convention with a real _superusers table.
    """

    __tablename__ = "_superusers"

    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    avatar: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_key: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_reset_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("email", name="uq_superusers_email"),
        UniqueConstraint("token_key", name="uq_superusers_token_key"),
    )


# ---------------------------------------------------------------------------
# _params
# ---------------------------------------------------------------------------


class ParamRecord(Base):
    """Key-value store for system settings (JWT secrets, app config, etc.)."""

    __tablename__ = "_params"

    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict | None] = mapped_column(  # type: ignore[type-arg]
        JSONB,
        nullable=True,
    )
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("key", name="uq_params_key"),
    )


# ---------------------------------------------------------------------------
# _external_auths
# ---------------------------------------------------------------------------


class ExternalAuthRecord(Base):
    """Links auth-collection records to external OAuth2 providers."""

    __tablename__ = "_external_auths"

    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    collection_id: Mapped[str] = mapped_column(String(15), nullable=False)
    record_id: Mapped[str] = mapped_column(String(15), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "provider",
            "provider_id",
            name="uq_external_auths_provider",
        ),
    )


# ---------------------------------------------------------------------------
# _requests (request logs)
# ---------------------------------------------------------------------------


class RequestLogRecord(Base):
    """Stores HTTP request logs for the admin logs UI."""

    __tablename__ = "_requests"

    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    exec_time: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0"
    )
    remote_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    referer: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# _migrations
# ---------------------------------------------------------------------------


class MigrationRecord(Base):
    """Tracks which migrations have been applied to the database."""

    __tablename__ = "_migrations"

    file: Mapped[str] = mapped_column(String(255), primary_key=True)
    applied: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# Table creation helper
# ---------------------------------------------------------------------------


async def create_system_tables(engine: AsyncEngine) -> None:
    """Create all system tables if they do not already exist.

    This is a convenience for bootstrapping a new database.  In production,
    Alembic migrations should be used instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
