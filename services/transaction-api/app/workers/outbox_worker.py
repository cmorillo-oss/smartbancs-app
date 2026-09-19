"""Worker del outbox: proceso APARTE que entrega los eventos pendientes.

Se ejecuta con:  python -m app.workers.outbox_worker   (servicio `outbox-worker` del compose)

ES LA RED DE SEGURIDAD del patrón Outbox: si la tarea de fondo de la API no llegó a entregar un
evento (IA caída, proceso reiniciado, mamparo lleno...), el evento sigue PENDING en la BD y este
worker lo recoge, con reintentos y backoff. Como es un proceso separado, tiene su propio pool de
conexiones y su propio circuit breaker: un problema aquí no afecta al camino de las transferencias.

Alcance de la Fase 5: entrega los eventos 'ai.transaction_created'. El despacho por lotes de
'bancs.balance_updated' se registra en HANDLED_EVENT_TYPES en la Fase 6 (cuando exista Bancs).
Mientras tanto esos eventos quedan PENDING, intactos, sin consumir reintentos.
"""
import asyncio
import signal
import time
import uuid

from prometheus_client import start_http_server

from app.config import settings
from app.database import SessionFactory, engine
from app.observability.logging import configure_logging, get_logger, trace_id_var
from app.observability.metrics import OUTBOX_PENDING
from app.repositories import outbox_repository as outbox
from app.services import ai_client

configure_logging()
log = get_logger("outbox_worker")

HANDLED_EVENT_TYPES = ["ai.transaction_created"]


async def _deliver(event, sem: asyncio.Semaphore) -> str:
    # Un semáforo limita las entregas en paralelo dentro del lote: 100 llamadas a la IA a la vez
    # serían justo el tipo de avalancha que el circuit breaker y el mamparo existen para evitar.
    async with sem:
        payload = event["payload"]
        return await ai_client.deliver_ai_event(
            event_id=event["id"], retry_count=event["retry_count"],
            transaction_id=uuid.UUID(str(event["aggregate_id"])),
            customer_id=payload["customer_id"], trace_id=event["trace_id"] or "-",
        )


async def run_cycle() -> dict:
    """Un ciclo: recuperar vencidos -> reclamar lote -> entregar -> actualizar gauge."""
    stats = {"claimed": 0, "sent": 0, "retry": 0, "dead": 0, "released": 0, "recovered": 0}

    async with SessionFactory() as session, session.begin():
        stats["recovered"] = await outbox.recover_stale(session)

    # Si el circuito hacia la IA está abierto NO reclamamos nada: reclamar y fallar consumiría
    # reintentos de eventos que no tienen culpa y los llevaría a DEAD durante una caída larga.
    # Se quedan PENDING y se entregan cuando la IA vuelva.
    if ai_client.breaker.would_allow():
        async with SessionFactory() as session, session.begin():
            batch = await outbox.claim_batch(session, HANDLED_EVENT_TYPES, settings.outbox_batch_size)
        stats["claimed"] = len(batch)
        if batch:
            sem = asyncio.Semaphore(settings.outbox_concurrency)
            # Cada entrega corre en su propia tarea => su propio contexto (trace_id aislado por evento).
            results = await asyncio.gather(*(_deliver(e, sem) for e in batch))
            for r in results:
                stats[r] += 1
    else:
        log.info("outbox_cycle_skipped_circuit_open")

    async with SessionFactory() as session:
        pending = await outbox.count_pending(session)
    OUTBOX_PENDING.set(pending)
    stats["pending"] = pending
    return stats


async def main() -> None:
    # Un evento de parada permite terminar el ciclo en curso antes de salir (docker stop envía SIGTERM).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    start_http_server(settings.metrics_port)  # /metrics del worker, para Prometheus
    ai_client.init_client()
    trace_id_var.set("-")
    log.info("outbox_worker_started", poll_interval_s=settings.outbox_poll_interval_s, batch=settings.outbox_batch_size)

    while not stop.is_set():
        started = time.perf_counter()
        try:
            stats = await run_cycle()
            if stats["claimed"] or stats["recovered"]:
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
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
