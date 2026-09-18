"""Punto de entrada de transaction-api (Fases 1-2: salud + observabilidad)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.database import engine
from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import register_pool_gauge, register_query_timing
from app.observability.middleware import TraceMiddleware
from app.observability.tracing import setup_tracing

# El logging se configura ANTES de crear la app: cualquier línea que emitan las librerías
# durante el arranque ya debe salir en JSON.
configure_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("service_started")
    yield
    log.info("service_stopping")
    # Al apagar, cerramos el pool ordenadamente: Postgres no se queda con conexiones huérfanas.
    await engine.dispose()


app = FastAPI(title="SmartBancs Transaction API", version="0.2.0", lifespan=lifespan)

# Orden importa: Starlette pone el ÚLTIMO middleware añadido como el más externo. Registramos
# TraceMiddleware primero y la instrumentación OTel después, para que OTel quede por fuera:
# así, cuando TraceMiddleware corre, ya existe un span activo al que adjuntar el trace_id.
app.add_middleware(TraceMiddleware)
setup_tracing(app, engine)
register_pool_gauge(engine)
register_query_timing(engine)


@app.get("/health")
async def health() -> dict:
    """Liveness: "el proceso está vivo".

    POR QUÉ NO toca la BD: si /health consultara Postgres, una caída de la BD haría que el
    orquestador reinicie la API en bucle, empeorando el incidente. Liveness y readiness
    responden preguntas distintas y por eso son endpoints separados.
    """
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """Readiness: "puedo atender tráfico ahora mismo" (la BD responde).

    Devuelve 503 si la BD falla para que un balanceador saque esta instancia de rotación
    sin matarla.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        # Se loguea el detalle (con trace_id) pero NO se devuelve al cliente: podría filtrar host/credenciales.
        log.exception("readiness_check_failed")
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "database": "unreachable"}
        )
    return {"status": "ready", "database": "connected"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Endpoint que Prometheus rastrea (scrape). Fuera del esquema OpenAPI: es para máquinas."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
