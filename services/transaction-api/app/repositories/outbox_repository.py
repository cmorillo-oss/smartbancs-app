"""Acceso a outbox_events y ai_recommendations.

PATRÓN "RECLAMAR, PROCESAR, CONFIRMAR" (claim-process-ack):
  1. RECLAMAR: una sentencia corta pasa el evento a PROCESSING (con un "lease" de caducidad).
  2. PROCESAR: se llama al servicio externo FUERA de cualquier transacción de BD.
  3. CONFIRMAR: otra transacción corta lo marca SENT, o lo devuelve a PENDING con backoff, o DEAD.
POR QUÉ así y no mantener FOR UPDATE abierto durante la llamada: una transacción abierta esperando
a un servicio lento retiene una conexión del pool y bloqueos (viola la regla de oro 1).
"""
import json
import random
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


async def claim_by_id(session: AsyncSession, event_id: int):
    """Reclama UN evento concreto si sigue PENDING (lo usa la tarea de fondo tras el commit).
    Es atómico: si el worker se adelantó, devuelve None y la tarea de fondo no hace nada (evita duplicados)."""
    row = await session.execute(
        text(
            """
            UPDATE outbox_events
               SET status = 'PROCESSING', next_retry_at = NOW() + make_interval(secs => :lease)
             WHERE id = :id AND status = 'PENDING'
         RETURNING id, aggregate_id, payload, retry_count, trace_id
            """
        ),
        {"id": event_id, "lease": settings.outbox_lease_s},
    )
    return row.mappings().first()


async def claim_batch(session: AsyncSession, event_types: list[str], limit: int):
    """Reclama hasta `limit` eventos PENDING listos. Una sola sentencia: dentro, FOR UPDATE SKIP LOCKED.

    SKIP LOCKED: si hay N workers, cada uno salta las filas que otro ya está reclamando en lugar de
    esperar. Así se escala horizontalmente sin contención ni duplicados.
    """
    rows = await session.execute(
        text(
            """
            WITH picked AS (
                SELECT id FROM outbox_events
                 WHERE status = 'PENDING'
                   AND event_type = ANY(:types)
                   AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                 ORDER BY id
                 LIMIT :lim
                   FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_events o
               SET status = 'PROCESSING', next_retry_at = NOW() + make_interval(secs => :lease)
              FROM picked
             WHERE o.id = picked.id
         RETURNING o.id, o.aggregate_id, o.event_type, o.payload, o.retry_count, o.trace_id
            """
        ),
        {"types": event_types, "lim": limit, "lease": settings.outbox_lease_s},
    )
    return rows.mappings().all()


async def recover_stale(session: AsyncSession) -> int:
    """Devuelve a PENDING los eventos cuyo lease caducó (el proceso que los reclamó murió).
    Sin esto, un crash entre 'reclamar' y 'confirmar' dejaría el evento atascado para siempre."""
    result = await session.execute(
        text(
            """
            UPDATE outbox_events SET status = 'PENDING', next_retry_at = NULL
             WHERE status = 'PROCESSING' AND next_retry_at < NOW()
            """
        )
    )
    return result.rowcount


async def mark_sent(session: AsyncSession, event_id: int) -> None:
    await session.execute(
        text("UPDATE outbox_events SET status = 'SENT', processed_at = NOW(), next_retry_at = NULL WHERE id = :id"),
        {"id": event_id},
    )


async def mark_failed(session: AsyncSession, event_id: int, retry_count: int) -> str:
    """Registra un fallo: backoff exponencial con jitter, y tras `outbox_max_retries` pasa a DEAD (dead letter).
    Devuelve el estado resultante."""
    attempts = retry_count + 1
    if attempts >= settings.outbox_max_retries:
        await session.execute(
            text(
                """
                UPDATE outbox_events
                   SET status = 'DEAD', retry_count = :n, processed_at = NOW(), next_retry_at = NULL
                 WHERE id = :id
                """
            ),
            {"id": event_id, "n": attempts},
        )
        return "DEAD"
    # 2s, 4s, 8s, 16s... con jitter +-25%: evita que todos los eventos fallidos reintenten a la vez
    # (thundering herd) justo cuando el servicio externo intenta recuperarse.
    delay = settings.outbox_backoff_base_s * (2 ** retry_count) * random.uniform(0.75, 1.25)
    await session.execute(
        text(
            """
            UPDATE outbox_events
               SET status = 'PENDING', retry_count = :n, next_retry_at = NOW() + make_interval(secs => :delay)
             WHERE id = :id
            """
        ),
        {"id": event_id, "n": attempts, "delay": delay},
    )
    return "PENDING"


async def release(session: AsyncSession, event_id: int) -> None:
    """Devuelve el evento a PENDING SIN contar un intento: no llegó a intentarse (circuito abierto)."""
    await session.execute(
        text("UPDATE outbox_events SET status = 'PENDING', next_retry_at = NULL WHERE id = :id AND status = 'PROCESSING'"),
        {"id": event_id},
    )


async def count_pending(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT count(*) FROM outbox_events WHERE status = 'PENDING'"))).scalar_one()


async def customer_history(session: AsyncSession, customer_id: str, limit: int = 50) -> list[dict]:
    """Últimas transferencias SALIENTES del cliente (lectura de la BD local, nunca de Bancs)."""
    rows = await session.execute(
        text(
            """
            SELECT t.id::text AS transaction_id, t.amount::text AS amount, t.currency,
                   d.account_number AS dest_account, t.created_at
              FROM transactions t
              JOIN accounts s ON s.id = t.source_account_id
              JOIN accounts d ON d.id = t.dest_account_id
             WHERE s.customer_id = :c AND t.status = 'COMPLETED'
             ORDER BY t.created_at DESC
             LIMIT :lim
            """
        ),
        {"c": customer_id, "lim": limit},
    )
    return [
        {**r, "created_at": r["created_at"].isoformat()} for r in rows.mappings().all()
    ]


async def insert_recommendation(
    session: AsyncSession, *, customer_id: str, transaction_id: uuid.UUID, recommendation: dict,
    model_version: str, latency_ms: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO ai_recommendations (customer_id, transaction_id, recommendation, model_version, latency_ms)
            VALUES (:c, :tx, CAST(:reco AS jsonb), :mv, :lat)
            """
        ),
        {"c": customer_id, "tx": transaction_id, "reco": json.dumps(recommendation), "mv": model_version, "lat": latency_ms},
    )


async def latest_recommendation(session: AsyncSession, customer_id: str):
    row = await session.execute(
        text(
            """
            SELECT recommendation, model_version, created_at FROM ai_recommendations
             WHERE customer_id = :c ORDER BY id DESC LIMIT 1
            """
        ),
        {"c": customer_id},
    )
    return row.mappings().first()
