"""Gancho de notificación a la IA.

FASE 3: solo el punto de enganche. El cliente real (httpx async, timeout de 1s, circuit breaker,
fallback) se implementa en la Fase 5.

Lo importante YA queda fijado: esta función se ejecuta en BackgroundTasks, después del commit y
de enviar la respuesta HTTP. Por eso, aunque la IA tarde 800ms o esté caída, el cliente nunca lo nota.
"""
import uuid

from app.observability.logging import get_logger

log = get_logger("ai_client")


async def notify_transaction(transaction_id: uuid.UUID, customer_id: str) -> None:
    try:
        # Fase 5: aquí irá la llamada real. Si falla, se loguea y ya: el evento
        # 'ai.transaction_created' del outbox es la red de seguridad (se reenviará).
        log.info("ai_notification_deferred", transaction_id=str(transaction_id), customer_id=customer_id)
    except Exception:
        # Un fallo en una tarea de fondo JAMÁS debe propagarse ni afectar a la transferencia ya confirmada.
        log.exception("ai_notification_failed", transaction_id=str(transaction_id))
