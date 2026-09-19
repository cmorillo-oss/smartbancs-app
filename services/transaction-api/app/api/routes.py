"""Endpoints HTTP de la API v1."""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.errors import AccountNotFound
from app.observability.logging import trace_id_var
from app.repositories import account_repository as accounts
from app.repositories import outbox_repository as outbox
from app.repositories import transaction_repository as txs
from app.schemas import (
    IDEMPOTENCY_KEY_PATTERN,
    AccountResponse,
    LedgerEntryResponse,
    TransactionDetail,
    TransactionListItem,
    TransactionPage,
    TransferRequest,
    TransferResponse,
)
from app.services import ai_client, transfer_service

router = APIRouter(prefix="/api/v1")


@router.post("/transactions", response_model=TransferResponse, status_code=201)
async def create_transaction(
    body: TransferRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    # Obligatoria: sin ella el cliente no puede reintentar de forma segura tras un timeout de red
    # (no sabría si su transferencia se aplicó o no).
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    if not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
        raise HTTPException(status_code=422, detail="Idempotency-Key inválida (1-64 caracteres: letras, dígitos, - o _)")

    result = await transfer_service.execute_transfer(body, idempotency_key, trace_id_var.get())

    if result.replay:
        # Paso 2: se devuelve lo almacenado, con 200 (no 201: no se creó nada nuevo) y la cabecera.
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    elif settings.ai_notify_in_process:
        # ---- Paso 10: DESPUÉS del commit, fuera de la transacción, sin esperar a la IA ------
        # BackgroundTasks se ejecuta cuando la respuesta HTTP ya salió hacia el cliente:
        # la IA (300-800ms) no puede sumar ni un milisegundo a la latencia de la transferencia.
        # Si la IA está caída o lenta, esta tarea falla en silencio: el evento sigue en el outbox.
        background_tasks.add_task(
            ai_client.notify_transaction,
            result.ai_event_id, result.data["transaction_id"], result.source_customer_id, trace_id_var.get(),
        )
    return result.data


@router.get("/transactions/{transaction_id}", response_model=TransactionDetail)
async def get_transaction(transaction_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    row = await txs.get_by_id(session, transaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="transacción no encontrada")
    entries = await txs.ledger_for_transaction(session, transaction_id)
    return {**row, "ledger_entries": [LedgerEntryResponse(**e) for e in entries]}


@router.get("/accounts/{account_number}", response_model=AccountResponse)
async def get_account(account_number: str, session: AsyncSession = Depends(get_session)):
    # Lectura directa a la BD LOCAL: nunca al core legado Bancs (restricción 2 del reto).
    row = await accounts.get_by_number(session, account_number)
    if row is None:
        raise AccountNotFound(f"cuenta inexistente: {account_number}")
    return row


@router.get("/accounts/{account_number}/transactions", response_model=TransactionPage)
async def get_account_transactions(
    account_number: str,
    # Tope de 100: sin él, un cliente podría pedir el historial completo y monopolizar una conexión del pool.
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    account_id = await txs.get_account_id(session, account_number)
    if account_id is None:
        raise AccountNotFound(f"cuenta inexistente: {account_number}")
    rows = await txs.list_for_account(session, account_id, limit, offset)
    page = rows[:limit]
    items = [
        TransactionListItem(
            **r, direction="DEBIT" if r["source_account"] == account_number else "CREDIT"
        )
        for r in page
    ]
    return TransactionPage(items=items, limit=limit, offset=offset, has_more=len(rows) > limit)


@router.get("/customers/{customer_id}/recommendations")
async def get_customer_recommendations(customer_id: str, session: AsyncSession = Depends(get_session)):
    """Última recomendación de IA del cliente, leída de la BD LOCAL.

    POR QUÉ lee de la BD y no llama a la IA: la IA tarda 300-800ms y puede estar caída; el cliente
    ve al instante lo último que la IA calculó (de forma asíncrona). Si no hay ninguna todavía
    (cliente nuevo, o la IA lleva caída desde antes), se devuelve la recomendación genérica en
    memoria: el fallback. Así este endpoint responde SIEMPRE, con la IA encendida o apagada.
    """
    row = await outbox.latest_recommendation(session, customer_id)
    if row is None:
        return {"customer_id": customer_id, "source": "fallback", **ai_client.FALLBACK_RECOMMENDATION}
    return {
        "customer_id": customer_id, "source": "ai", "model_version": row["model_version"],
        "generated_at": row["created_at"], **row["recommendation"],
    }
