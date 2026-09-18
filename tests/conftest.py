import asyncpg
import pytest_asyncio

from common import DATABASE_DSN, new_client


@pytest_asyncio.fixture
async def db():
    """Conexión directa a Postgres: las pruebas verifican el ESTADO REAL de la BD,
    no solo lo que la API dice (una API con bug podría mentir; la tabla no)."""
    conn = await asyncpg.connect(DATABASE_DSN)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def client():
    async with new_client() as c:
        yield c
