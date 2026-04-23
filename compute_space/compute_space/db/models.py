"""SQLAlchemy ORM models for the compute_space schema.

These are the single source of truth for the database schema; Alembic
autogenerate compares live DB state against this metadata.
"""

from sqlalchemy import CheckConstraint
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column


class Base(DeclarativeBase):
    pass


class App(Base):
    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    manifest_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'serverfull'"))
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_check: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_port: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    container_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    docker_container_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'stopped'"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("128"))
    cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    gpu: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    public_paths: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'[]'"))
    manifest_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("(datetime('now'))"))
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("(datetime('now'))"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('building', 'starting', 'running', 'stopped', 'error')",
            name="status_check",
        ),
        Index("idx_apps_status", "status"),
    )


class AppDatabase(Base):
    __tablename__ = "app_databases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(Text, ForeignKey("apps.name", ondelete="CASCADE"), nullable=False)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    db_path: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("app_name", "db_name"),)


class AppPortMapping(Base):
    __tablename__ = "app_port_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(Text, ForeignKey("apps.name", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    container_port: Mapped[int] = mapped_column(Integer, nullable=False)
    host_port: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("app_name", "label"),
        Index("idx_port_mappings_host_port", "host_port", unique=True),
    )


class Owner(Base):
    __tablename__ = "owner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_needs_set: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("(datetime('now'))"))

    __table_args__ = (CheckConstraint("id = 1", name="owner_singleton"),)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    revoked: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (Index("idx_refresh_tokens_token_hash", "token_hash"),)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("(datetime('now'))"))


class AppToken(Base):
    __tablename__ = "app_tokens"

    app_name: Mapped[str] = mapped_column(
        Text,
        ForeignKey("apps.name", ondelete="CASCADE"),
        primary_key=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class ServiceProvider(Base):
    __tablename__ = "service_providers"

    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    app_name: Mapped[str] = mapped_column(Text, ForeignKey("apps.name", ondelete="CASCADE"), nullable=False)

    __table_args__ = (PrimaryKeyConstraint("service_name", "app_name"),)


class Permission(Base):
    __tablename__ = "permissions"

    consumer_app: Mapped[str] = mapped_column(Text, ForeignKey("apps.name", ondelete="CASCADE"), nullable=False)
    permission_key: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("consumer_app", "permission_key"),)


__all__ = [
    "Base",
    "App",
    "AppDatabase",
    "AppPortMapping",
    "Owner",
    "RefreshToken",
    "ApiToken",
    "AppToken",
    "ServiceProvider",
    "Permission",
]
