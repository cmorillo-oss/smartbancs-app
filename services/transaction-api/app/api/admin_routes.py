"""Endpoints administrativos (operación y diagnóstico)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.services import reconciliation_service

router = APIRouter(prefix="/api/v1/admin")


@router.get("/reconciliation")
async def reconciliation(
    limit: int = Query(20, ge=1, le=settings.reconciliation_max_accounts, description="cuentas a comprobar (las de actividad más reciente)"),
    accounts: str | None = Query(None, description="lista separada por comas; si se indica, ignora `limit`"),
    session: AsyncSession = Depends(get_session),
):
    """Compara saldos locales contra Bancs. Resultado por cuenta:
    MATCH | PENDING_SYNC (diferencia esperada: hay cambios sin sincronizar) | DISCREPANCY (real) | NOT_IN_BANCS | BANCS_ERROR.
    """
    numbers = [a.strip() for a in accounts.split(",") if a.strip()][: settings.reconciliation_max_accounts] if accounts else None
    return await reconciliation_service.reconcile(session, limit, numbers)
