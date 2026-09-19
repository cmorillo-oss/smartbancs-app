"""Cliente de la IA: SIEMPRE fuera del camino crítico de la transferencia.

Garantías de diseño (regla de oro 2: la IA nunca bloquea la transferencia):
  * Se invoca desde BackgroundTasks, DESPUÉS del commit y de enviar la respuesta HTTP.
  * Timeout duro de 1s: una IA lenta no acumula tareas colgadas.
  * Circuit breaker: con la IA caída, no se intenta ni la llamada (falla rápido).
  * Mamparo (bulkhead): tope de llamadas simultáneas; si se llena, se omite (el outbox reenviará).
  * Ninguna conexión de BD se mantiene abierta durante la llamada HTTP (regla de oro 1).
  * Nada de aquí propaga excepciones: un fallo de la IA NUNCA puede afectar a una transferencia ya confirmada.
"""
import asyncio
import time
import uuid

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import SessionFactory
from app.observability.logging import get_logger, trace_id_var
from app.observability.metrics import AI_CALL_DURATION, AI_CALLS_TOTAL, AI_CIRCUIT_BREAKER_STATE
from app.repositories import outbox_repository as outbox
from app.services.circuit_breaker import CircuitBreaker

log = get_logger("ai_client")

breaker = CircuitBreaker(
    failure_threshold=settings.ai_breaker_failure_threshold,
    recovery_s=settings.ai_breaker_recovery_s,
    on_state_change=lambda s: (
        AI_CIRCUIT_BREAKER_STATE.set(int(s)),
        log.warning("ai_circuit_breaker_state_changed", state=s.name),
    ),
)

# Recomendación genérica "cacheada": vive en memoria desde el arranque, sin depender de la IA ni de la BD.
# Es lo que se devuelve cuando la IA no está disponible.
FALLBACK_RECOMMENDATION = {
    "model_version": "fallback",
    "recommendations": [{
        "type": "generic_saving_tip", "severity": "info", "title": "Consejo general de ahorro",
        "message": "Revisa tus gastos del mes y considera apartar una parte de cada ingreso para tu fondo de emergencia.",
        "evidence": {},
    }],
}

_client: httpx.AsyncClient | None = None
# Mamparo: se crea perezosamente para atarlo al event loop en ejecución.
_bulkhead: asyncio.Semaphore | None = None


def init_client() -> None:
    global _client, _bulkhead
    _client = httpx.AsyncClient(
        base_url=settings.ai_service_url,
        timeout=httpx.Timeout(settings.ai_timeout_s),
        limits=httpx.Limits(max_connections=settings.ai_max_concurrency + 10, max_keepalive_connections=20),
    )
    _bulkhead = asyncio.Semaphore(settings.ai_max_concurrency)


async def close_client() -> None:
    if _client is not None:
        await _client.aclose()


async def call_ai(customer_id: str, history: list[dict], trace_id: str) -> tuple[str, dict | None]:
    """Llama a la IA con breaker + timeout duro. Devuelve (estado, respuesta).

    estado: "ok" | "failed" | "circuit_open". Nunca lanza excepciones.
    """
    if not breaker.allow_request():
        AI_CALLS_TOTAL.labels(status="circuit_open").inc()
        return "circuit_open", None

    started = time.perf_counter()
    try:
        # httpx.Timeout limita cada fase (conexión, lectura...) por separado; asyncio.timeout pone
        # un tope al TOTAL. Ambos: así "1s" significa 1s de verdad, no "1s por fase".
        async with asyncio.timeout(settings.ai_timeout_s):
            resp = await _client.post(
                "/api/v1/recommendations",
                json={"customer_id": customer_id, "transaction_history": history, "trace_id": trace_id},
                headers={"X-Trace-Id": trace_id},  # el trace_id viaja hacia la IA: mismo id en los logs de ambos servicios
            )
        AI_CALL_DURATION.observe(time.perf_counter() - started)
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError("error del servidor de IA", request=resp.request, response=resp)
        resp.raise_for_status()
    except (TimeoutError, httpx.TimeoutException) as exc:
        AI_CALL_DURATION.observe(time.perf_counter() - started)
        AI_CALLS_TOTAL.labels(status="timeout").inc()
        breaker.record_failure()
        log.warning("ai_call_timeout", customer_id=customer_id, error=type(exc).__name__)
        return "failed", None
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de la IA se degrada, nunca se propaga
        AI_CALLS_TOTAL.labels(status="failure").inc()
        breaker.record_failure()
        log.warning("ai_call_failed", customer_id=customer_id, error=type(exc).__name__, detail=str(exc)[:200])
        return "failed", None

    AI_CALLS_TOTAL.labels(status="success").inc()
    breaker.record_success()
    return "ok", resp.json()


async def deliver_ai_event(event_id: int, retry_count: int, transaction_id: uuid.UUID, customer_id: str, trace_id: str) -> str:
    """Entrega UN evento 'ai.transaction_created' ya reclamado (PROCESSING). Lo comparten la tarea de
    fondo y el worker. Devuelve "sent" | "retry" | "dead" | "released"."""
    trace_id_var.set(trace_id)  # los logs de esta entrega llevan el trace_id de la transferencia original
    try:
        # 1) Leer historial: sesión CORTA, cerrada ANTES de llamar a la IA (no retener conexión durante 300-800ms).
        async with SessionFactory() as session:
            history = await outbox.customer_history(session, customer_id)

        # 2) Llamada HTTP: sin ninguna conexión de BD abierta.
        status, data = await call_ai(customer_id, history, trace_id)

        # 3) Confirmar el resultado en una transacción corta.
        async with SessionFactory() as session, session.begin():
            if status == "ok":
                await outbox.insert_recommendation(
                    session, customer_id=customer_id, transaction_id=transaction_id,
                    recommendation=data, model_version=data.get("model_version", "unknown"),
                    latency_ms=data.get("simulated_latency_ms", 0),
                )
                await outbox.mark_sent(session, event_id)
                log.info("ai_event_delivered", event_id=event_id, transaction_id=str(transaction_id))
                return "sent"
            if status == "circuit_open":
                await outbox.release(session, event_id)  # no se intentó: no consume reintentos
                return "released"
            result = await outbox.mark_failed(session, event_id, retry_count)
            log.warning("ai_event_failed", event_id=event_id, next_state=result, attempts=retry_count + 1)
            return "dead" if result == "DEAD" else "retry"
    except (SQLAlchemyError, OSError) as exc:
        # La BD falló al confirmar: el lease caducará y el evento se recuperará solo (recover_stale).
        log.error("ai_event_ack_failed", event_id=event_id, error=type(exc).__name__)
        return "retry"


async def notify_transaction(event_id: int, transaction_id: uuid.UUID, customer_id: str, trace_id: str) -> None:
    """Tarea de fondo (paso 10 del algoritmo): se ejecuta tras el commit y tras responder al cliente."""
    try:
        # Si el circuito está abierto, NO tocamos ni la BD: contar y seguir. El evento queda PENDING
        # en el outbox y el worker lo entregará cuando la IA se recupere.
        if not breaker.would_allow():
            AI_CALLS_TOTAL.labels(status="circuit_open").inc()
            log.info("ai_notification_skipped_circuit_open", transaction_id=str(transaction_id))
            return
        # Mamparo lleno: no encolamos más tareas esperando a la IA; el outbox es la red de seguridad.
        if _bulkhead is None or _bulkhead.locked():
            AI_CALLS_TOTAL.labels(status="skipped_bulkhead").inc()
            return
        async with _bulkhead:
            async with SessionFactory() as session, session.begin():
                claimed = await outbox.claim_by_id(session, event_id)
            if claimed is None:  # el worker ya lo reclamó: no duplicar
                return
            await deliver_ai_event(event_id, claimed["retry_count"], transaction_id, customer_id, trace_id)
    except Exception:  # noqa: BLE001 - una tarea de fondo JAMÁS propaga errores
        log.exception("ai_notification_failed", transaction_id=str(transaction_id))
