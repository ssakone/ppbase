"""Shared pytest fixtures for PPBase integration tests.

Uses either:
- PPBASE_TEST_DATABASE_URL env var (if set) — run against existing DB
- testcontainers PostgreSQL (otherwise) — disposable instance

Example with local DB:
  python -m ppbase db start
  PPBASE_TEST_DATABASE_URL=postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase pytest tests/
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# PostgreSQL URL
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_url() -> Generator[str, None, None]:
    """Provide a PostgreSQL URL for tests.

    Uses PPBASE_TEST_DATABASE_URL if set, otherwise starts a testcontainer.
    """
    env_url = os.environ.get("PPBASE_TEST_DATABASE_URL")
    if env_url:
        yield env_url
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        image="postgres:16-alpine",
        username="pptest",
        password="pptest",
        dbname="pptest",
    ) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        url = f"postgresql+asyncpg://pptest:pptest@{host}:{port}/pptest"
        yield url


# ---------------------------------------------------------------------------
# App + client
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def app_client(pg_url: str) -> AsyncGenerator[AsyncClient, None]:
    """Create the FastAPI app with the test database and yield an httpx client."""
    from ppbase.config import Settings
    from ppbase.app import create_app
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.db.system_tables import create_system_tables
    from ppbase.db.bootstrap import bootstrap_system_collections
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    settings = Settings(
        database_url=pg_url,
        jwt_secret="test-secret-key-for-integration-tests",
        auto_migrate=False,
    )
    app = create_app(settings)

    # Manually initialize engine and system tables
    engine = await init_engine(pg_url, pool_size=5, max_overflow=5)
    await create_system_tables(engine)

    # Bootstrap all system collections
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await bootstrap_system_collections(session, engine)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    await close_engine()


# ---------------------------------------------------------------------------
# Admin setup
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def admin_token(app_client: AsyncClient, pg_url: str) -> str:
    """Create an admin account (or login if one exists) and return a valid admin JWT.

    Uses PPBASE_TEST_ADMIN_EMAIL and PPBASE_TEST_ADMIN_PASSWORD when set.
    If init fails (admin exists) and login fails, creates a test admin directly
    in the DB so tests can run against a shared database.
    """
    email = os.environ.get("PPBASE_TEST_ADMIN_EMAIL", "admin@test.com")
    password = os.environ.get("PPBASE_TEST_ADMIN_PASSWORD", "adminpass123")

    resp = await app_client.post(
        "/api/admins/init",
        json={
            "email": email,
            "password": password,
            "passwordConfirm": password,
        },
    )
    if resp.status_code == 200:
        return resp.json()["token"]

    # Admin exists — try login
    login_resp = await app_client.post(
        "/api/admins/auth-with-password",
        json={"identity": email, "password": password},
    )
    if login_resp.status_code == 200:
        return login_resp.json()["token"]

    # Login failed — ensure test admin exists (create or update password)
    from ppbase.db.engine import get_engine
    from ppbase.services.admin_service import create_admin, get_admin_by_email
    from ppbase.services.auth_service import create_admin_token, hash_password, generate_token_key
    from ppbase.db.system_tables import SuperuserRecord
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    engine = get_engine()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        admin = await get_admin_by_email(session, email)
        if admin:
            admin.password_hash = hash_password(password)
            admin.token_key = generate_token_key()
            session.add(admin)
        else:
            admin = await create_admin(session, email, password)
        await session.commit()

        # Look up the _superusers collection for per-collection secret
        from ppbase.db.system_tables import CollectionRecord
        su_stmt = select(CollectionRecord).where(CollectionRecord.name == "_superusers")
        su_coll = (await session.execute(su_stmt)).scalars().first()

        class _TestSettings:
            def get_jwt_secret(self):
                return "test-secret-key-for-integration-tests"
            admin_token_duration = 86400
        settings = _TestSettings()
        token = create_admin_token(admin, settings, superusers_collection=su_coll)
    return token


# ---------------------------------------------------------------------------
# Auth collection setup
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def auth_collection(
    app_client: AsyncClient,
    admin_token: str,
) -> dict:
    """Get the bootstrapped ``users`` auth collection and set rules to public for tests."""
    # The users collection is auto-created by bootstrap; fetch it
    get_resp = await app_client.get(
        "/api/collections/users",
        headers={"Authorization": admin_token},
    )
    if get_resp.status_code == 200:
        coll = get_resp.json()
        # Ensure public rules for test convenience
        patch_resp = await app_client.patch(
            f"/api/collections/{coll['id']}",
            headers={"Authorization": admin_token},
            json={
                "listRule": "",
                "viewRule": "",
                "createRule": "",
                "updateRule": "",
                "deleteRule": "",
            },
        )
        if patch_resp.status_code == 200:
            return patch_resp.json()
        return coll

    # Fallback: create if not found (shouldn't happen with bootstrap)
    resp = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": "test_users",
            "type": "auth",
            "schema": [
                {"name": "name", "type": "text", "required": False},
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert resp.status_code == 200, f"Collection create failed: {resp.text}"
    return resp.json()
