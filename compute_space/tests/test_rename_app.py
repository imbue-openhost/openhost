"""Tests for the rename_app DB cascade helper.

Exercises ``_rename_app_in_db`` against an on-disk SQLite with the real
``_enable_sqlite_pragmas`` listener — so ``PRAGMA foreign_keys=ON`` is in force
and a regression that left the parent/child rename out of sync would trip a
``FOREIGN KEY constraint failed`` IntegrityError.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from compute_space.db.connection import dispose_engine
from compute_space.db.connection import init_engine
from compute_space.db.models import App
from compute_space.db.models import AppDatabase
from compute_space.db.models import AppPortMapping
from compute_space.db.models import AppToken
from compute_space.db.models import Base
from compute_space.db.models import Permission
from compute_space.db.models import ServiceProvider
from compute_space.web.routes.api.apps import _rename_app_in_db


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    """On-disk SQLite engine with the production pragma listener (FKs ON)."""
    db_path = tmp_path / "rename.db"
    eng = init_engine(str(db_path))
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await dispose_engine()


@pytest.mark.asyncio
async def test_rename_app_cascades_to_all_child_tables(engine: AsyncEngine) -> None:
    old = "myapp"
    new = "renamed"

    async with engine.begin() as conn:
        await conn.execute(
            insert(App).values(
                name=old,
                manifest_name=old,
                version="1",
                runtime_type="serverfull",
                repo_path=f"/srv/apps/{old}/main",
                local_port=9500,
            )
        )
        await conn.execute(
            insert(AppDatabase).values(app_name=old, db_name="main", db_path=f"/srv/data/{old}/main.db")
        )
        await conn.execute(
            insert(AppPortMapping).values(app_name=old, label="metrics", container_port=9090, host_port=19600)
        )
        await conn.execute(insert(AppToken).values(app_name=old, token_hash="abc123"))
        await conn.execute(insert(ServiceProvider).values(service_name="mailer", app_name=old))
        await conn.execute(insert(Permission).values(consumer_app=old, permission_key="email.send"))

    await _rename_app_in_db(engine, old, new)

    async with engine.connect() as conn:
        app_rows = (await conn.execute(select(App).where(App.name == new))).all()
        assert len(app_rows) == 1
        assert app_rows[0].repo_path == f"/srv/apps/{new}/main"

        assert (await conn.execute(select(App).where(App.name == old))).first() is None

        dbs = (await conn.execute(select(AppDatabase).where(AppDatabase.app_name == new))).all()
        assert len(dbs) == 1
        assert dbs[0].db_path == f"/srv/data/{new}/main.db"

        mappings = (await conn.execute(select(AppPortMapping).where(AppPortMapping.app_name == new))).all()
        assert len(mappings) == 1
        assert mappings[0].host_port == 19600

        tokens = (await conn.execute(select(AppToken).where(AppToken.app_name == new))).all()
        assert len(tokens) == 1
        assert tokens[0].token_hash == "abc123"

        svcs = (await conn.execute(select(ServiceProvider).where(ServiceProvider.app_name == new))).all()
        assert len(svcs) == 1
        assert svcs[0].service_name == "mailer"

        perms = (await conn.execute(select(Permission).where(Permission.consumer_app == new))).all()
        assert len(perms) == 1
        assert perms[0].permission_key == "email.send"

        for table, col in [
            (AppDatabase, AppDatabase.app_name),
            (AppPortMapping, AppPortMapping.app_name),
            (AppToken, AppToken.app_name),
            (ServiceProvider, ServiceProvider.app_name),
            (Permission, Permission.consumer_app),
        ]:
            leftover = (await conn.execute(select(table).where(col == old))).all()
            assert leftover == [], f"{table.__tablename__} still has rows pointing at {old}"
