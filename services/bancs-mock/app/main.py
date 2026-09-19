"""Mock del core legado "Bancs": lento y frágil ante la concurrencia, a propósito.

Sirve para hacer VISIBLE por qué SmartBancs nunca lo consulta en caliente:
  * Latencia base de 200-500 ms por petición.
  * Con más de BANCS_MAX_CONCURRENT peticiones simultáneas, la latencia crece linealmente
    y empieza a devolver 503 (cada vez con más probabilidad).
Su propio /metrics muestra el throughput y las peticiones en vuelo.
"""
import asyncio
import random
import time
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from app.config import settings
from app.logging import configure_logging, get_logger, trace_id_var

configure_logging()
log = get_logger("bancs")

REQUESTS = Counter("bancs_requests_total", "Peticiones recibidas por Bancs", ["endpoint", "status"])
IN_FLIGHT = Gauge("bancs_in_flight_requests", "Peticiones en curso ahora mismo")
DURATION = Histogram(
    "bancs_request_duration_seconds", "Duración de las peticiones a Bancs", ["endpoint"],
    buckets=(.1, .25, .5, 1, 2, 3, 5, 10),
)
EVENTS_APPLIED = Counter("bancs_balance_events_applied_total", "Eventos de saldo aplicados")

app = FastAPI(title="Bancs (core legado simulado)", version="1.0.0")

# Estado en memoria: saldos por cuenta y último "sequence" aplicado por cuenta.
# Precargado con las MISMAS cuentas que database/dml/03_seed.sql: el legado conoce a todos los
# clientes desde antes; SmartBancs solo le sincroniza los cambios.
BALANCES: dict[str, Decimal] = {"ACC-TEST-CONCURRENCY": Decimal("1000.00"), "ACC-TEST-SINK": Decimal("0.00")}
BALANCES.update({f"ACC-{g:06d}": Decimal(g * 5000) for g in range(1, 19)})
LAST_SEQUENCE: dict[str, int] = {}
_in_flight = 0


@asynccontextmanager
async def load_gate(endpoint: str):
    """Simula el comportamiento del legado bajo carga. Cuenta las peticiones simultáneas y
    aplica latencia base + degradación lineal + fallos 503 cuando se supera el máximo."""
    global _in_flight
    _in_flight += 1
    IN_FLIGHT.set(_in_flight)
    started = time.perf_counter()
    status = "200"
    try:
        excess = max(0, _in_flight - settings.bancs_max_concurrent)
        lo, hi = settings.latency_range_ms()
        delay_ms = random.randint(lo, hi) + excess * settings.bancs_degradation_ms_per_excess
        await asyncio.sleep(delay_ms / 1000)
        # Probabilidad de fallo proporcional al exceso: con 2x la capacidad, casi todo falla.
        if excess and random.random() < min(0.95, excess / settings.bancs_max_concurrent):
            status = "503"
            log.warning("bancs_overloaded", in_flight=_in_flight, excess=excess, endpoint=endpoint)
            raise HTTPException(status_code=503, detail="Bancs saturado")
        try:
            yield
        except HTTPException as exc:  # p. ej. 404 de una cuenta desconocida: se contabiliza con su código real
            status = str(exc.status_code)
            raise
    finally:
        _in_flight -= 1
        IN_FLIGHT.set(_in_flight)
        REQUESTS.labels(endpoint=endpoint, status=status).inc()
        DURATION.labels(endpoint=endpoint).observe(time.perf_counter() - started)


class BalanceEvent(BaseModel):
    sequence: int  # id del evento del outbox: orden total de los cambios
    transaction_id: str
    source_account: str
    dest_account: str
    amount: str
    currency: str = "USD"
    source_balance_after: str
    dest_balance_after: str
    trace_id: str | None = None


class BalanceBatch(BaseModel):
    batch_id: str
    events: list[BalanceEvent] = Field(max_length=500)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "in_flight": _in_flight}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/bancs/v1/accounts/balance/batch")
async def apply_balance_batch(batch: BalanceBatch):
    """Recibe un LOTE de cambios de saldo (hasta 500; SmartBancs envía 100).

    IDEMPOTENTE: cada evento fija el saldo ABSOLUTO resultante y solo se aplica si su `sequence`
    es mayor que la última aplicada a esa cuenta. Así un reenvío (at-least-once del outbox) o un
    evento que llega desordenado nunca corrompe el saldo.
    """
    trace_id_var.set(f"bancs-batch-{batch.batch_id[:8]}")
    async with load_gate("balance_batch"):
        applied = stale = 0
        for ev in sorted(batch.events, key=lambda e: e.sequence):  # en orden de secuencia, pase lo que pase
            for account, after in ((ev.source_account, ev.source_balance_after), (ev.dest_account, ev.dest_balance_after)):
                if ev.sequence > LAST_SEQUENCE.get(account, 0):
                    BALANCES[account] = Decimal(after)
                    LAST_SEQUENCE[account] = ev.sequence
                    applied += 1
                else:
                    stale += 1
        EVENTS_APPLIED.inc(len(batch.events))
        log.info("batch_applied", batch_id=batch.batch_id, events=len(batch.events), balances_applied=applied, stale_skipped=stale)
        return {"batch_id": batch.batch_id, "events": len(batch.events), "balances_applied": applied, "stale_skipped": stale}


@app.get("/bancs/v1/accounts/{account_number}")
async def get_account(account_number: str):
    """Consulta individual de saldo: lo único que SmartBancs hace de lectura, y solo en la conciliación."""
    trace_id_var.set(f"bancs-get-{account_number}")
    async with load_gate("get_account"):
        balance = BALANCES.get(account_number)
        if balance is None:
            raise HTTPException(status_code=404, detail="cuenta desconocida en Bancs")
        return {"account_number": account_number, "balance": str(balance), "last_sequence": LAST_SEQUENCE.get(account_number, 0)}
