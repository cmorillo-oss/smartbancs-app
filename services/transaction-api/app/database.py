"""Motores y fábrica de sesiones async de SQLAlchemy."""
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
    # application_name aparece en pg_stat_activity: en un incidente permite saber QUÉ servicio
    # (API, worker, diagnóstico...) tiene cada conexión y cada bloqueo, sin adivinar por la IP.
    connect_args={"server_settings": {"application_name": settings.service_name}},
)

# expire_on_commit=False: tras el commit los objetos siguen legibles sin lanzar un SELECT
# implícito, que en async provocaría un error (lazy-load no permitido).
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)

# ---- Pool APARTE para diagnóstico ---------------------------------------------------------------
# POR QUÉ un pool propio: cuando el pool principal se agota (justo el incidente que hay que
# diagnosticar) un endpoint de diagnóstico que usara ese mismo pool también se quedaría esperando
# y el operador estaría a ciegas cuando más lo necesita. Este motor tiene 2 conexiones reservadas
# solo para diagnóstico, con un statement_timeout corto para que una consulta lenta no las acapare.
diag_engine = create_async_engine(
    settings.database_url,
    pool_size=2,
    max_overflow=0,
    pool_timeout=3,
    connect_args={"server_settings": {
        "application_name": f"{settings.service_name}-diagnostics",
        "statement_timeout": "5000",
    }},
)
DiagSessionFactory = async_sessionmaker(diag_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia de FastAPI: una sesión por petición, siempre cerrada al terminar
    (devuelve la conexión al pool aunque la petición falle)."""
    async with SessionFactory() as session:
        yield session


async def get_diag_session() -> AsyncIterator[AsyncSession]:
    async with DiagSessionFactory() as session:
        yield session
