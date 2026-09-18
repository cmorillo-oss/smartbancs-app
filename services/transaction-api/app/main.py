"""Punto de entrada de transaction-api (Fase 1: solo salud)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.database import engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Al apagar, cerramos el pool ordenadamente: Postgres no se queda con conexiones huérfanas.
    await engine.dispose()


app = FastAPI(title="SmartBancs Transaction API", version="0.1.0", lifespan=lifespan)


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
        # No devolvemos el texto de la excepción: podría filtrar host/credenciales al cliente.
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "database": "unreachable"}
        )
    return {"status": "ready", "database": "connected"}
