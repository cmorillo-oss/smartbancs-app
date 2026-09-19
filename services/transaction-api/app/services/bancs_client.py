"""Cliente del core legado Bancs.

REGLA CENTRAL DEL RETO: Bancs se degrada con consultas directas, así que ESTE MÓDULO NUNCA SE USA EN
EL CAMINO CRÍTICO. Solo lo llaman (1) el worker del outbox, que envía cambios en LOTES y a ritmo
controlado, y (2) la conciliación administrativa, limitada en cantidad y concurrencia.
"""
import asyncio
import time
from decimal import Decimal

import httpx

from app.config import settings
from app.observability.logging import get_logger
from app.observability.metrics import BANCS_CIRCUIT_BREAKER_STATE
from app.services.circuit_breaker import CircuitBreaker

log = get_logger("bancs_client")

# Breaker PROPIO de Bancs (independiente del de la IA): si el legado está saturado dejamos de
# enviarle lotes un rato para que se recupere en vez de empeorar su situación.
breaker = CircuitBreaker(
    failure_threshold=settings.bancs_breaker_failure_threshold,
    recovery_s=settings.bancs_breaker_recovery_s,
    on_state_change=lambda s: (
        BANCS_CIRCUIT_BREAKER_STATE.set(int(s)),
        log.warning("bancs_circuit_breaker_state_changed", state=s.name),
    ),
)

_client: httpx.AsyncClient | None = None


def init_client() -> None:
    global _client
    _client = httpx.AsyncClient(
        base_url=settings.bancs_url,
        timeout=httpx.Timeout(settings.bancs_timeout_s),
        # Pocas conexiones a propósito: Bancs no soporta concurrencia; no debemos ser nosotros quienes lo saturen.
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


async def close_client() -> None:
    if _client is not None:
        await _client.aclose()


async def send_batch(batch_id: str, events: list[dict]) -> tuple[str, int, str | None]:
    """Envía un lote. Devuelve (estado, latencia_ms, error): estado = "ok" | "failed" | "circuit_open"."""
    if not breaker.allow_request():
        return "circuit_open", 0, None
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.bancs_timeout_s):
            resp = await _client.post("/bancs/v1/accounts/balance/batch", json={"batch_id": batch_id, "events": events})
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError(f"Bancs respondió {resp.status_code}", request=resp.request, response=resp)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - cualquier fallo se degrada y se reintenta desde el outbox
        latency_ms = int((time.perf_counter() - started) * 1000)
        breaker.record_failure()
        return "failed", latency_ms, f"{type(exc).__name__}: {str(exc)[:200]}"
    breaker.record_success()
    return "ok", latency_ms, None


async def get_balance(account_number: str) -> tuple[str, Decimal | None]:
    """Consulta individual (solo conciliación). Devuelve ("ok", saldo) | ("not_found", None) | ("failed", None) | ("circuit_open", None)."""
    if not breaker.allow_request():
        return "circuit_open", None
    try:
        async with asyncio.timeout(settings.bancs_timeout_s):
            resp = await _client.get(f"/bancs/v1/accounts/{account_number}")
        if resp.status_code == 404:
            breaker.record_success()  # una cuenta desconocida no indica que Bancs esté mal
            return "not_found", None
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError("Bancs saturado", request=resp.request, response=resp)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        breaker.record_failure()
        log.warning("bancs_get_failed", account=account_number, error=type(exc).__name__)
        return "failed", None
    breaker.record_success()
    return "ok", Decimal(resp.json()["balance"])
