"""Unit tests for port allocation and availability checking."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from compute_space.core.manifest import PortMapping
from compute_space.core.ports import check_port_available
from compute_space.core.ports import resolve_port_mappings
from compute_space.db.models import App
from compute_space.db.models import AppPortMapping
from compute_space.db.models import Base


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """In-memory async SQLite session with the full ORM schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add_app(session: AsyncSession, name: str, local_port: int) -> None:
    session.add(
        App(
            name=name,
            manifest_name=name,
            version="1",
            runtime_type="serverfull",
            repo_path="/repo",
            local_port=local_port,
        )
    )
    await session.commit()


async def _add_mapping(session: AsyncSession, app_name: str, label: str, container_port: int, host_port: int) -> None:
    session.add(AppPortMapping(app_name=app_name, label=label, container_port=container_port, host_port=host_port))
    await session.commit()


class TestCheckPortAvailable:
    @pytest.mark.asyncio
    async def test_free_port_is_available(self, session):
        # Use a high ephemeral port unlikely to be in use
        available, _used_by = await check_port_available(59123, session)
        # May or may not be available depending on host, but should not error
        assert isinstance(available, bool)

    @pytest.mark.asyncio
    async def test_port_used_by_app_main(self, session):
        await _add_app(session, "myapp", 9500)
        available, used_by = await check_port_available(9500, session)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["type"] == "main_port"

    @pytest.mark.asyncio
    async def test_port_used_by_mapping(self, session):
        await _add_app(session, "myapp", 9500)
        await _add_mapping(session, "myapp", "metrics", 9090, 9600)
        available, used_by = await check_port_available(9600, session)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["label"] == "metrics"
        assert used_by["type"] == "port_mapping"

    @pytest.mark.asyncio
    async def test_exclude_app_skips_own_main_port(self, session):
        await _add_app(session, "myapp", 9500)
        available, _used_by = await check_port_available(9500, session, exclude_app="myapp")
        assert available is True

    @pytest.mark.asyncio
    async def test_exclude_app_skips_own_mapping(self, session):
        await _add_app(session, "myapp", 9500)
        await _add_mapping(session, "myapp", "metrics", 9090, 9600)
        available, _used_by = await check_port_available(9600, session, exclude_app="myapp")
        assert available is True

    @pytest.mark.asyncio
    async def test_exclude_app_still_blocks_other_app(self, session):
        await _add_app(session, "other", 9500)
        available, _used_by = await check_port_available(9500, session, exclude_app="myapp")
        assert available is False


class TestResolvePortMappings:
    @pytest.mark.asyncio
    async def test_fixed_ports_pass_through(self, session):
        mappings = [PortMapping(label="web", container_port=80, host_port=59200)]
        resolved = await resolve_port_mappings(mappings, session, 59200, 59300)
        assert len(resolved) == 1
        assert resolved[0].host_port == 59200

    @pytest.mark.asyncio
    async def test_auto_assign_picks_free_port(self, session):
        mappings = [PortMapping(label="auto", container_port=3000, host_port=0)]
        resolved = await resolve_port_mappings(mappings, session, 59200, 59300)
        assert len(resolved) == 1
        assert 59200 <= resolved[0].host_port <= 59300

    @pytest.mark.asyncio
    async def test_conflict_with_db_raises(self, session):
        await _add_app(session, "other", 9500)
        mappings = [PortMapping(label="conflict", container_port=80, host_port=9500)]
        with pytest.raises(RuntimeError, match="already in use"):
            await resolve_port_mappings(mappings, session)

    @pytest.mark.asyncio
    async def test_duplicate_port_in_batch_raises(self, session):
        mappings = [
            PortMapping(label="a", container_port=80, host_port=59200),
            PortMapping(label="b", container_port=81, host_port=59200),
        ]
        with pytest.raises(RuntimeError, match="multiple mappings"):
            await resolve_port_mappings(mappings, session, 59200, 59300)

    @pytest.mark.asyncio
    async def test_mixed_fixed_and_auto(self, session):
        mappings = [
            PortMapping(label="fixed", container_port=80, host_port=59200),
            PortMapping(label="auto", container_port=3000, host_port=0),
        ]
        resolved = await resolve_port_mappings(mappings, session, 59200, 59300)
        assert len(resolved) == 2
        ports = {r.label: r.host_port for r in resolved}
        assert ports["fixed"] == 59200
        assert ports["auto"] != 59200  # should pick different port
        assert 59200 <= ports["auto"] <= 59300
