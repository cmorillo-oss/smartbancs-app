"""Endpoints de diagnóstico para el operador (requisito 3.5: identificar el cuello de botella en segundos)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_diag_session
from app.errors import DomainError
from app.services import diagnostics_service

router = APIRouter(prefix="/api/v1/admin/diagnostics", tags=["diagnostics"])


class DiagnosticsUnavailable(DomainError):
    status_code = 503
    error_code = "DIAGNOSTICS_UNAVAILABLE"


@router.get("/slow-queries")
async def slow_queries(limit: int = Query(10, ge=1, le=50), session: AsyncSession = Depends(get_diag_session)):
    """Consultas que más tiempo total consumen (pg_stat_statements)."""
    return await diagnostics_service.slow_queries(session, limit)


@router.get("/locks")
async def locks(session: AsyncSession = Depends(get_diag_session)):
    """Sesiones bloqueadas y bloqueantes (pg_locks + pg_stat_activity), con quién bloquea a quién."""
    return await diagnostics_service.locks(session)


@router.get("/blocking-tree")
async def blocking_tree(session: AsyncSession = Depends(get_diag_session)):
    """Árbol de bloqueos: la raíz es el culpable; sus hijos, las sesiones que está frenando."""
    return await diagnostics_service.blocking_tree(session)


@router.get("/pool")
async def pool():
    """Estado del pool de conexiones. Tolerante a fallos: siempre devuelve el estado local del pool."""
    from app.database import DiagSessionFactory
    try:
        async with DiagSessionFactory() as session:
            return await diagnostics_service.pool(session)
    except Exception:  # noqa: BLE001 - si ni el pool de diagnóstico responde, se informa solo el lado API
        return await diagnostics_service.pool(None)
