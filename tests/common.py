"""Utilidades compartidas por las pruebas de concurrencia y el script comparativo."""
import asyncio
import json
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000")
DATABASE_DSN = os.environ.get(
    "DATABASE_DSN", "postgresql://smartbancs:smartbancs@localhost:5432/smartbancs"
)
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", "evidence/test-data"))


def save_evidence(name: str, data: dict) -> None:
    """Guarda el resultado de cada prueba como JSON: evidencia reproducible para el jurado."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{name}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def new_client() -> httpx.AsyncClient:
    # Límites altos: queremos que las 100 peticiones viajen REALMENTE a la vez; con el límite
    # por defecto de httpx (100 conexiones, keepalive 20) el propio cliente serializaría la carga.
    return httpx.AsyncClient(
        base_url=API_URL,
        timeout=60.0,
        limits=httpx.Limits(max_connections=300, max_keepalive_connections=300),
    )


async def create_accounts(conn, count: int, balance: Decimal, prefix: str = "T4") -> list[dict]:
    """Crea cuentas NUEVAS con saldo conocido.

    POR QUÉ cuentas propias por prueba: cada test es independiente y repetible, no depende
    del estado que dejó otro test ni de que la BD esté "limpia".
    """
    run = uuid.uuid4().hex[:8]
    accounts = []
    for i in range(count):
        number = f"{prefix}-{run}-{i:02d}"
        acc_id = await conn.fetchval(
            "INSERT INTO accounts (account_number, customer_id, balance, currency) "
            "VALUES ($1, $2, $3, 'USD') RETURNING id",
            number, f"CUST-{prefix}-{run}", balance,
        )
        accounts.append({"id": acc_id, "number": number})
    return accounts


async def api_transfer(client: httpx.AsyncClient, src: str, dst: str, amount: Decimal, key: str | None = None):
    # El monto viaja como string: jamás como float (0.1 no es representable en binario).
    return await client.post(
        "/api/v1/transactions",
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
        json={"source_account": src, "dest_account": dst, "amount": str(amount), "currency": "USD"},
    )


async def total_balance(conn, account_ids: list[int]) -> Decimal:
    return await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM accounts WHERE id = ANY($1)", account_ids)


async def deadlocks_counter(client: httpx.AsyncClient) -> float:
    """Lee smartbancs_db_deadlocks_total del /metrics de la API."""
    text = (await client.get("/metrics")).text
    m = re.search(r"^smartbancs_db_deadlocks_total (\S+)", text, re.MULTILINE)
    return float(m.group(1)) if m else 0.0


async def run_concurrently(coros, limit: int | None = None):
    """Lanza todas las corrutinas a la vez (o con un tope de concurrencia si se indica)."""
    if limit is None:
        return await asyncio.gather(*coros)
    sem = asyncio.Semaphore(limit)

    async def guarded(c):
        async with sem:
            return await c

    return await asyncio.gather(*(guarded(c) for c in coros))
