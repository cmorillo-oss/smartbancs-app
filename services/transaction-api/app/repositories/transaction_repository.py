"""Acceso a transactions, ledger_entries y outbox_events."""
import json
import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Consulta base que devuelve una transacción con los NÚMEROS de cuenta (no los ids internos:
# los ids son detalle de implementación y no deben filtrarse a la API).
_TX_SELECT = """
    SELECT t.id AS transaction_id, t.idempotency_key, t.status, t.amount, t.currency,
           t.trace_id, t.created_at, t.completed_at, t.error_code,
           s.account_number AS source_account, d.account_number AS dest_account
      FROM transactions t
      JOIN accounts s ON s.id = t.source_account_id
      JOIN accounts d ON d.id = t.dest_account_id
"""


async def find_by_idempotency_key(session: AsyncSession, key: str):
    row = await session.execute(text(_TX_SELECT + " WHERE t.idempotency_key = :k"), {"k": key})
    return row.mappings().first()


async def get_by_id(session: AsyncSession, tx_id: uuid.UUID):
    row = await session.execute(text(_TX_SELECT + " WHERE t.id = :id"), {"id": tx_id})
    return row.mappings().first()


async def insert_transaction(
    session: AsyncSession,
    *,
    tx_id: uuid.UUID,
    idempotency_key: str,
    source_id: int,
    dest_id: int,
    amount: Decimal,
    currency: str,
    trace_id: str,
):
    """Inserta la transacción ya COMPLETED (la operación es atómica: no existe estado intermedio visible)."""
    row = await session.execute(
        text(
            """
            INSERT INTO transactions
                (id, idempotency_key, source_account_id, dest_account_id, amount, currency,
                 status, trace_id, completed_at)
            VALUES (:id, :key, :src, :dst, :amount, :currency, 'COMPLETED', :trace_id, NOW())
            RETURNING created_at, completed_at
            """
        ),
        {
            "id": tx_id, "key": idempotency_key, "src": source_id, "dst": dest_id,
            "amount": amount, "currency": currency, "trace_id": trace_id,
        },
    )
    return row.mappings().one()


async def insert_ledger_pair(
    session: AsyncSession,
    *,
    tx_id: uuid.UUID,
    source_id: int,
    dest_id: int,
    amount: Decimal,
    source_balance_after: Decimal,
    dest_balance_after: Decimal,
) -> None:
    """Partida doble: un DEBIT en el origen y un CREDIT en el destino, mismo monto y misma transacción."""
    await session.execute(
        text(
            """
            INSERT INTO ledger_entries (transaction_id, account_id, entry_type, amount, balance_after)
            VALUES (:tx, :src, 'DEBIT',  :amount, :src_after),
                   (:tx, :dst, 'CREDIT', :amount, :dst_after)
            """
        ),
        {
            "tx": tx_id, "src": source_id, "dst": dest_id, "amount": amount,
            "src_after": source_balance_after, "dst_after": dest_balance_after,
        },
    )


async def insert_outbox_event(
    session: AsyncSession, *, aggregate_id: uuid.UUID, event_type: str, payload: dict, trace_id: str
) -> None:
    # default=str: Decimal y UUID no son serializables por defecto; se guardan como texto
    # (los montos como string evitan perder precisión al pasar por JSON).
    await session.execute(
        text(
            """
            INSERT INTO outbox_events (aggregate_id, event_type, payload, trace_id)
            VALUES (:agg, :etype, CAST(:payload AS jsonb), :trace_id)
            """
        ),
        {
            "agg": aggregate_id, "etype": event_type,
            "payload": json.dumps(payload, default=str), "trace_id": trace_id,
        },
    )


async def list_for_account(session: AsyncSession, account_id: int, limit: int, offset: int):
    # Pedimos limit+1 filas para saber si hay más páginas SIN un COUNT(*) (que recorre todo el historial).
    rows = await session.execute(
        text(
            _TX_SELECT
            + """
             WHERE t.source_account_id = :acc OR t.dest_account_id = :acc
             ORDER BY t.created_at DESC, t.id DESC     -- id desempata: paginación estable
             LIMIT :lim OFFSET :off
            """
        ),
        {"acc": account_id, "lim": limit + 1, "off": offset},
    )
    return rows.mappings().all()


async def ledger_for_transaction(session: AsyncSession, tx_id: uuid.UUID):
    rows = await session.execute(
        text(
            """
            SELECT a.account_number, l.entry_type, l.amount, l.balance_after
              FROM ledger_entries l JOIN accounts a ON a.id = l.account_id
             WHERE l.transaction_id = :tx ORDER BY l.id
            """
        ),
        {"tx": tx_id},
    )
    return rows.mappings().all()


async def get_account_id(session: AsyncSession, account_number: str) -> int | None:
    row = await session.execute(
        text("SELECT id FROM accounts WHERE account_number = :n"), {"n": account_number}
    )
    return row.scalar_one_or_none()
