"""Motor y fábrica de sesiones async de SQLAlchemy."""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# POR QUÉ async: con miles de peticiones concurrentes, un hilo bloqueado esperando a la BD
# es un hilo desperdiciado. Con asyncio una sola corrutina espera sin ocupar el event loop.
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    # pool_pre_ping: verifica que la conexión siga viva antes de entregarla. Cuesta un
    # round-trip mínimo pero evita errores en peticiones reales tras un reinicio de Postgres.
    pool_pre_ping=True,
)

# expire_on_commit=False: tras el commit los objetos siguen legibles sin lanzar un SELECT
# implícito, que en async provocaría un error (lazy-load no permitido).
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia de FastAPI: una sesión por petición, siempre cerrada al terminar
    (devuelve la conexión al pool aunque la petición falle)."""
    async with SessionFactory() as session:
        yield session
