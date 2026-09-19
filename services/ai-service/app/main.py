"""Servicio de IA (mock avanzado): recomendaciones financieras con latencia y fallos configurables."""
import asyncio
import random
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Header, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from app import engine
from app.config import settings
from app.logging import configure_logging, get_logger, trace_id_var

configure_logging()
log = get_logger("ai")

REQUESTS = Counter("smartbancs_ai_service_requests_total", "Peticiones de recomendación", ["status"])
DURATION = Histogram(
    "smartbancs_ai_service_request_duration_seconds", "Duración de /recommendations",
    buckets=(.05, .1, .25, .5, .75, 1, 2, 5),
)

app = FastAPI(title="SmartBancs AI Service", version="1.0.0")


class HistoryItem(BaseModel):
    transaction_id: str | None = None
    amount: str  # como texto: los montos jamás viajan como float
    currency: str = "USD"
    dest_account: str | None = None
    description: str | None = None
    category: str | None = None
    created_at: datetime | None = None


class RecommendationRequest(BaseModel):
    customer_id: str
    transaction_history: list[HistoryItem] = Field(default_factory=list, max_length=500)
    trace_id: str | None = None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model_version": settings.model_version}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/recommendations")
async def recommendations(body: RecommendationRequest, x_trace_id: str | None = Header(default=None)):
    # Propaga el trace_id recibido (cuerpo o cabecera) a TODOS los logs de esta petición.
    trace_id_var.set(body.trace_id or x_trace_id or "-")
    started = time.perf_counter()

    # Latencia deliberada: es la justificación empírica de por qué la IA NO puede ser síncrona.
    lo, hi = settings.latency_range_ms()
    delay_ms = random.randint(lo, hi)
    await asyncio.sleep(delay_ms / 1000)

    # Fallo inyectable para demos de resiliencia (se decide DESPUÉS de esperar, como un servicio real que se cae tarde).
    if random.random() < settings.ai_failure_rate:
        REQUESTS.labels(status="error").inc()
        DURATION.observe(time.perf_counter() - started)
        log.warning("recommendation_failed_injected", customer_id=body.customer_id)
        return JSONResponse(status_code=503, content={"error": "simulated_failure", "trace_id": trace_id_var.get()})

    result = engine.generate(body.customer_id, [h.model_dump(mode="json") for h in body.transaction_history])
    result.update({
        "trace_id": trace_id_var.get(),
        "model_version": settings.model_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "simulated_latency_ms": delay_ms,
    })
    REQUESTS.labels(status="ok").inc()
    DURATION.observe(time.perf_counter() - started)
    log.info(
        "recommendation_generated", customer_id=body.customer_id,
        history=len(body.transaction_history), recommendations=len(result["recommendations"]),
        latency_ms=delay_ms,
    )
    return result
