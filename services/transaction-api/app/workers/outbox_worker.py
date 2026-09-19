"""Worker del outbox: proceso APARTE que entrega los eventos pendientes.

Se ejecuta con:  python -m app.workers.outbox_worker   (servicio `outbox-worker` del compose)

ES LA RED DE SEGURIDAD del patrón Outbox: cada cambio de saldo dejó sus eventos en la BD dentro de
la misma transacción; este proceso los lleva a sus destinos con reintentos y backoff:
  * 'ai.transaction_created'  -> servicio de IA (uno a uno, con concurrencia limitada)
  * 'bancs.balance_updated'   -> core legado Bancs, en LOTES de hasta 100 (Fase 6)

Como es un proceso separado tiene su propio pool de conexiones y sus propios circuit breakers: un
problema con Bancs o la IA no afecta al camino de las transferencias. Se puede escalar a N réplicas
gracias a FOR UPDATE SKIP LOCKED.
"""
import asyncio
import signal
import time
import uuid

from prometheus_client import start_http_server

from app.config import settings
from app.database import SessionFactory, engine
from app.observability.logging import configure_logging, get_logger, trace_id_var
from app.observability.metrics import BANCS_SYNC_BATCH_SIZE, BANCS_SYNC_DURATION, BANCS_SYNC_TOTAL, OUTBOX_PENDING
from app.repositories import outbox_repository as outbox
from app.services import ai_client, bancs_client

configure_logging()
log = get_logger("outbox_worker")

AI_EVENT = "ai.transaction_created"
BANCS_EVENT = "bancs.balance_updated"


# --------------------------------------------------------------------------------------------
# IA
# --------------------------------------------------------------------------------------------
async def _deliver_ai(event, sem: asyncio.Semaphore) -> str:
    # Un semáforo limita las entregas en paralelo dentro del lote: 100 llamadas a la IA a la vez
    # serían justo el tipo de avalancha que el circuit breaker y el mamparo existen para evitar.
    async with sem:
        payload = event["payload"]
        return await ai_client.deliver_ai_event(
            event_id=event["id"], retry_count=event["retry_count"],
            transaction_id=uuid.UUID(str(event["aggregate_id"])),
            customer_id=payload["customer_id"], trace_id=event["trace_id"] or "-",
        )


async def sync_ai() -> dict:
    stats = {"ai_claimed": 0, "ai_sent": 0, "ai_retry": 0, "ai_dead": 0, "ai_released": 0}
    # Con el circuito de la IA abierto NO reclamamos: reclamar y fallar consumiría reintentos de
    # eventos sin culpa y los llevaría a DEAD durante una caída larga. Se quedan PENDING.
    if not ai_client.breaker.would_allow():
        log.info("ai_sync_skipped_circuit_open")
        return stats
    async with SessionFactory() as session, session.begin():
        batch = await outbox.claim_batch(session, [AI_EVENT], settings.outbox_batch_size)
    stats["ai_claimed"] = len(batch)
    if batch:
        sem = asyncio.Semaphore(settings.outbox_concurrency)
        # Cada entrega corre en su propia tarea => su propio contexto (trace_id aislado por evento).
        for r in await asyncio.gather(*(_deliver_ai(e, sem) for e in batch)):
            stats[f"ai_{r}"] += 1
    return stats


# --------------------------------------------------------------------------------------------
# BANCS: sincronización por lotes
# --------------------------------------------------------------------------------------------
async def sync_bancs() -> dict:
    """Envía a Bancs los cambios de saldo pendientes AGRUPADOS en lotes de hasta `outbox_batch_size`.

    POR QUÉ lotes: una petición a Bancs cuesta 200-500 ms sin importar cuántos eventos lleve. Enviar
    100 eventos en 1 petición cuesta lo mismo que enviar 1, y le presenta a Bancs 1 petición en vez
    de 100 (que lo saturarían). Con 10.000 TPS locales, Bancs recibe ~100 peticiones/s como mucho.

    "Cada 2 s o cuando el lote se llena": el ciclo corre cada 2 s (lote parcial), y si el lote sale
    lleno se envía otro de inmediato (hasta un tope por ciclo), sin esperar los 2 s.
    """
    stats = {"bancs_batches": 0, "bancs_events_sent": 0, "bancs_batches_failed": 0, "bancs_events_dead": 0}
    for _ in range(settings.bancs_max_batches_per_cycle):
        # Con el circuito de Bancs abierto no se reclama nada (mismo razonamiento que con la IA).
        if not bancs_client.breaker.would_allow():
            log.info("bancs_sync_skipped_circuit_open")
            break
        async with SessionFactory() as session, session.begin():
            claimed = await outbox.claim_batch(session, [BANCS_EVENT], settings.outbox_batch_size)
        if not claimed:
            break

        batch_id = uuid.uuid4()
        ids = [e["id"] for e in claimed]
        # trace_id del lote: un lote mezcla eventos de muchas transferencias, así que el lote tiene el
        # suyo propio y cada evento conserva el de su transferencia dentro del cuerpo (trazabilidad ambas vías).
        trace_id_var.set(f"bancs-batch-{str(batch_id)[:8]}")
        events = [
            {"sequence": e["id"], "trace_id": e["trace_id"], **e["payload"]} for e in claimed
        ]
        status, latency_ms, error = await bancs_client.send_batch(str(batch_id), events)

        # Confirmar el resultado del lote en UNA transacción corta (sin conexión abierta durante la llamada).
        async with SessionFactory() as session, session.begin():
            if status == "ok":
                await outbox.mark_sent_many(session, ids)
                await outbox.insert_sync_log(session, batch_id=batch_id, events_count=len(ids), status="SUCCESS",
                                             latency_ms=latency_ms, error_message=None)
            elif status == "failed":
                counts = await outbox.mark_failed_many(session, ids)
                stats["bancs_events_dead"] += counts.get("DEAD", 0)
                await outbox.insert_sync_log(session, batch_id=batch_id, events_count=len(ids), status="FAILED",
                                             latency_ms=latency_ms, error_message=error)
            else:  # circuit_open: no se intentó => los eventos vuelven intactos, sin gastar reintentos
                await outbox.release_many(session, ids)

        BANCS_SYNC_TOTAL.labels(status={"ok": "success", "failed": "failure", "circuit_open": "circuit_open"}[status]).inc()
        if status == "ok":
            BANCS_SYNC_BATCH_SIZE.observe(len(ids))
            BANCS_SYNC_DURATION.observe(latency_ms / 1000)
            stats["bancs_batches"] += 1
            stats["bancs_events_sent"] += len(ids)
            log.info("bancs_batch_sent", batch_id=str(batch_id), events=len(ids), latency_ms=latency_ms)
        elif status == "failed":
            stats["bancs_batches_failed"] += 1
            log.warning("bancs_batch_failed", batch_id=str(batch_id), events=len(ids), latency_ms=latency_ms, error=error)
            break  # Bancs está mal: no insistir con más lotes en este ciclo
        else:
            break
        if len(claimed) < settings.outbox_batch_size:
            break  # lote parcial: no hay más pendientes; el próximo ciclo (2 s) recogerá lo nuevo
    return stats


# --------------------------------------------------------------------------------------------
async def run_cycle() -> dict:
    """Un ciclo: recuperar vencidos -> (IA y Bancs EN PARALELO) -> actualizar gauge."""
    async with SessionFactory() as session, session.begin():
        recovered = await outbox.recover_stale(session)

    # En paralelo: entregar 100 eventos a la IA tarda varios segundos; no debe retrasar la sincronización con Bancs.
    ai_stats, bancs_stats = await asyncio.gather(sync_ai(), sync_bancs())

    async with SessionFactory() as session:
        pending = await outbox.count_pending(session)
    OUTBOX_PENDING.set(pending)
    return {"recovered": recovered, "pending": pending, **ai_stats, **bancs_stats}


async def main() -> None:
    # Un evento de parada permite terminar el ciclo en curso antes de salir (docker stop envía SIGTERM).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    start_http_server(settings.metrics_port)  # /metrics del worker, para Prometheus
    ai_client.init_client()
    bancs_client.init_client()
    trace_id_var.set("-")
    log.info("outbox_worker_started", poll_interval_s=settings.outbox_poll_interval_s, batch=settings.outbox_batch_size)

    while not stop.is_set():
        started = time.perf_counter()
        try:
            stats = await run_cycle()
            if any(stats[k] for k in ("ai_claimed", "bancs_batches", "bancs_batches_failed", "recovered")):
                log.info("outbox_cycle", **stats)
        except Exception:  # noqa: BLE001 - un ciclo fallido (p. ej. BD reiniciándose) no mata al worker
            log.exception("outbox_cycle_failed")
        # Dormir el resto del intervalo (no intervalo fijo + duración): cadencia estable de 2s.
        remaining = settings.outbox_poll_interval_s - (time.perf_counter() - started)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(remaining, 0.05))
        except asyncio.TimeoutError:
            pass

    log.info("outbox_worker_stopping")
    await ai_client.close_client()
    await bancs_client.close_client()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
