"""Servicio de transferencias: implementa el algoritmo de la Fase 3 paso a paso.

Reglas de oro que se cumplen aquí:
  1. Ninguna llamada externa (IA, Bancs) dentro de la transacción de BD.
  2. La IA nunca se espera: se dispara DESPUÉS del commit, en segundo plano (ver routes.py).
  3. Bloqueos de cuentas SIEMPRE en orden ascendente de id (ver account_repository).
  5. Idempotencia por `idempotency_key`.
"""
import asyncio
import random
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.config import settings
from app.database import SessionFactory
from app.errors import (
    AccountNotActive,
    AccountNotFound,
    CurrencyMismatch,
    DeadlockRetryExhausted,
    IdempotencyKeyConflict,
    InsufficientFunds,
    LockTimeout,
    PoolTimeout,
)
from app.observability.logging import get_logger
from app.observability.metrics import (
    DB_DEADLOCKS,
    DB_LOCK_WAIT,
    TRANSACTION_DURATION,
    TRANSACTION_ERRORS,
    TRANSACTIONS_TOTAL,
)
from app.repositories import account_repository as accounts
from app.repositories import transaction_repository as txs
from app.schemas import TransferRequest

log = get_logger("transfer")

# Códigos SQLSTATE de PostgreSQL relevantes
SQLSTATE_DEADLOCK = "40P01"          # deadlock_detected
SQLSTATE_LOCK_TIMEOUT = "55P03"      # lock_not_available (también lo lanza lock_timeout)
SQLSTATE_UNIQUE_VIOLATION = "23505"  # unique_violation


@dataclass
class TransferResult:
    data: dict          # forma de TransferResponse
    replay: bool        # True si es la respuesta almacenada de una petición anterior
    source_customer_id: str | None = None  # para alimentar a la IA (solo si replay=False)


def _sqlstate(exc: DBAPIError) -> str | None:
    """Extrae el SQLSTATE a través de las capas (SQLAlchemy -> adaptador -> asyncpg).

    POR QUÉ un helper defensivo: la ubicación exacta del atributo cambia según la versión del
    adaptador; comparar por SQLSTATE (estable en el estándar) es más robusto que comparar el texto del error.
    """
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        for attr in ("sqlstate", "pgcode"):
            code = getattr(candidate, attr, None)
            if code:
                return code
    return None


def _stored_to_response(row) -> dict:
    return {k: row[k] for k in (
        "transaction_id", "idempotency_key", "status", "source_account", "dest_account",
        "amount", "currency", "trace_id", "created_at", "completed_at",
    )}


def _same_request(row, key: str, req: TransferRequest) -> bool:
    return (
        row["source_account"] == req.source_account
        and row["dest_account"] == req.dest_account
        and row["amount"] == req.amount
        and row["currency"] == req.currency
    )


async def execute_transfer(req: TransferRequest, idempotency_key: str, trace_id: str) -> TransferResult:
    started = time.perf_counter()
    try:
        result = await _execute(req, idempotency_key, trace_id)
    except Exception as exc:
        # Métricas de error con código acotado (cardinalidad baja). Los errores no de dominio
        # se cuentan como INTERNAL_ERROR: no queremos que un bug pase inadvertido.
        code = getattr(exc, "error_code", "INTERNAL_ERROR")
        TRANSACTION_ERRORS.labels(error_code=code).inc()
        if code == "INSUFFICIENT_FUNDS":
            TRANSACTIONS_TOTAL.labels(status="FAILED", currency=req.currency).inc()
        raise
    finally:
        TRANSACTION_DURATION.labels(operation="transfer").observe(time.perf_counter() - started)

    if not result.replay:
        TRANSACTIONS_TOTAL.labels(status="COMPLETED", currency=req.currency).inc()
    return result


async def _execute(req: TransferRequest, idempotency_key: str, trace_id: str) -> TransferResult:
    # ---- Paso 2: idempotencia (camino rápido) --------------------------------------------
    # Consulta de solo lectura, sin bloqueos: un reintento del cliente se responde sin tocar
    # cuentas ni competir por locks. Sesión propia y corta: no retiene conexión durante el trabajo real.
    try:
        async with SessionFactory() as session:
            existing = await txs.find_by_idempotency_key(session, idempotency_key)
    except PoolTimeoutError:
        raise PoolTimeout("no hay conexiones disponibles en el pool")
    if existing:
        return _replay(existing, idempotency_key, req)

    # ---- Pasos 3-9 con reintento ante deadlock --------------------------------------------
    attempt = 0
    while True:
        attempt += 1
        attempt_started = time.perf_counter()
        try:
            return await _transaction_attempt(req, idempotency_key, trace_id)
        except DBAPIError as exc:
            code = _sqlstate(exc)

            if code == SQLSTATE_DEADLOCK:
                DB_DEADLOCKS.inc()
                # Log a nivel ERROR con todo lo necesario para diagnosticar (trace_id se añade solo).
                log.error(
                    "deadlock_detected",
                    source_account=req.source_account,
                    dest_account=req.dest_account,
                    query=(exc.statement or "").strip()[:500],
                    wait_seconds=round(time.perf_counter() - attempt_started, 3),
                    attempt=attempt,
                    error=str(exc.orig)[:300],
                )
                if attempt > settings.deadlock_max_retries:
                    raise DeadlockRetryExhausted(
                        "la transferencia no pudo completarse por contención; reintente"
                    )
                # Backoff exponencial + jitter: si dos transacciones chocaron, reintentar AL MISMO
                # TIEMPO las haría chocar otra vez. El azar las desincroniza.
                delay = settings.deadlock_backoff_base_ms / 1000 * (2 ** (attempt - 1))
                await asyncio.sleep(delay * (0.5 + random.random()))
                continue

            if code == SQLSTATE_LOCK_TIMEOUT:
                log.error(
                    "lock_timeout",
                    source_account=req.source_account,
                    dest_account=req.dest_account,
                    wait_seconds=round(time.perf_counter() - attempt_started, 3),
                )
                raise LockTimeout("tiempo de espera de bloqueo agotado; reintente")

            if code == SQLSTATE_UNIQUE_VIOLATION:
                # Carrera de idempotencia: otra petición con la MISMA clave se coló entre nuestra
                # comprobación y el INSERT. El UNIQUE de la BD es el árbitro final; la transacción
                # ya hizo rollback (no se movió dinero) y devolvemos lo que la ganadora guardó.
                async with SessionFactory() as session:
                    existing = await txs.find_by_idempotency_key(session, idempotency_key)
                if existing:
                    return _replay(existing, idempotency_key, req)
            raise
        except PoolTimeoutError:
            log.error("pool_timeout", source_account=req.source_account, dest_account=req.dest_account)
            raise PoolTimeout("no hay conexiones disponibles en el pool")


def _replay(existing, key: str, req: TransferRequest) -> TransferResult:
    if not _same_request(existing, key, req):
        raise IdempotencyKeyConflict("la Idempotency-Key ya se usó con un cuerpo distinto")
    log.info("idempotent_replay", transaction_id=str(existing["transaction_id"]))
    return TransferResult(data=_stored_to_response(existing), replay=True)


async def _transaction_attempt(req: TransferRequest, idempotency_key: str, trace_id: str) -> TransferResult:
    """Un intento completo: BEGIN ... COMMIT. Si algo falla, `session.begin()` hace ROLLBACK."""
    async with SessionFactory() as session:
        # ---- Paso 3: BEGIN TRANSACTION (READ COMMITTED) ---------------------------------
        # READ COMMITTED + FOR UPDATE: el bloqueo de fila serializa lo que importa (los saldos)
        # sin pagar el coste ni los fallos por serialización de niveles más altos. Tras obtener
        # el bloqueo, PostgreSQL relee la fila con su valor más reciente, así que el saldo que
        # validamos es el real.
        async with session.begin():
            await session.connection(execution_options={"isolation_level": "READ COMMITTED"})

            # set_config(..., true) equivale a SET LOCAL: el timeout aplica SOLO a esta
            # transacción y no "se filtra" a la siguiente que use la misma conexión del pool.
            # (Se usa set_config y no SET LOCAL porque SET no admite parámetros enlazados.)
            await session.execute(
                text("SELECT set_config('lock_timeout', :v, true)"),
                {"v": f"{settings.db_lock_timeout_ms}ms"},
            )

            # Resolver números -> ids (lectura sin bloqueo; ver docstring de resolve_ids).
            ids = await accounts.resolve_ids(session, [req.source_account, req.dest_account])
            for number in (req.source_account, req.dest_account):
                if number not in ids:
                    raise AccountNotFound(f"cuenta inexistente: {number}")
            source_id, dest_id = ids[req.source_account], ids[req.dest_account]

            # ---- Paso 4: ORDEN DETERMINISTA DE BLOQUEO -----------------------------------
            # (Explicación completa en account_repository.lock_accounts_in_order.)
            lock_started = time.perf_counter()
            locked = await accounts.lock_accounts_in_order(session, [source_id, dest_id])
            # Tiempo que esta petición esperó por los bloqueos: es EL indicador de contención.
            DB_LOCK_WAIT.observe(time.perf_counter() - lock_started)
            source, dest = locked[source_id], locked[dest_id]

            # Re-comprobación de idempotencia YA BLOQUEADOS. Si 50 peticiones con la misma clave
            # llegan a la vez, todas pasaron el paso 2 (aún no existía). Como los bloqueos las
            # serializan, la segunda en entrar aquí ya ve el commit de la primera y se detiene
            # sin mover dinero. (El UNIQUE de la BD sigue siendo la última red de seguridad.)
            existing = await txs.find_by_idempotency_key(session, idempotency_key)
            if existing:
                return _replay(existing, idempotency_key, req)

            # ---- Paso 5: reglas de negocio con datos YA bloqueados ------------------------
            # Validar ANTES de bloquear sería una condición de carrera: el saldo podría cambiar
            # entre la validación y el débito (el clásico check-then-act).
            if source.status != "ACTIVE":
                raise AccountNotActive(f"cuenta origen no activa: {source.account_number}")
            if dest.status != "ACTIVE":
                raise AccountNotActive(f"cuenta destino no activa: {dest.account_number}")
            if not (source.currency == dest.currency == req.currency):
                raise CurrencyMismatch("las divisas de las cuentas y de la transferencia deben coincidir")
            if source.balance < req.amount:
                raise InsufficientFunds(f"saldo insuficiente en {source.account_number}")

            # ---- Paso 6: mover el dinero --------------------------------------------------
            source_after = await accounts.apply_delta(session, source_id, -req.amount)
            dest_after = await accounts.apply_delta(session, dest_id, req.amount)

            # ---- Paso 7: transacción + partida doble ---------------------------------------
            tx_id = uuid.uuid4()
            times = await txs.insert_transaction(
                session, tx_id=tx_id, idempotency_key=idempotency_key, source_id=source_id,
                dest_id=dest_id, amount=req.amount, currency=req.currency, trace_id=trace_id,
            )
            await txs.insert_ledger_pair(
                session, tx_id=tx_id, source_id=source_id, dest_id=dest_id, amount=req.amount,
                source_balance_after=source_after, dest_balance_after=dest_after,
            )

            # ---- Paso 8: OUTBOX en la MISMA transacción -----------------------------------
            # Patrón Outbox: el evento se confirma atómicamente con el cambio de saldo. Si el
            # proceso muere después del COMMIT, el worker (Fase 5) lo encontrará y lo enviará:
            # entrega at-least-once SIN transacciones distribuidas (2PC) contra Bancs.
            # Nótese que aquí solo se ESCRIBE en nuestra BD; no se llama a nadie (regla de oro 1).
            await txs.insert_outbox_event(
                session, aggregate_id=tx_id, event_type="bancs.balance_updated", trace_id=trace_id,
                payload={
                    "transaction_id": tx_id, "source_account": source.account_number,
                    "dest_account": dest.account_number, "amount": req.amount,
                    "currency": req.currency, "source_balance_after": source_after,
                    "dest_balance_after": dest_after,
                },
            )
            await txs.insert_outbox_event(
                session, aggregate_id=tx_id, event_type="ai.transaction_created", trace_id=trace_id,
                payload={
                    "transaction_id": tx_id, "customer_id": source.customer_id,
                    "source_account": source.account_number, "dest_account": dest.account_number,
                    "amount": req.amount, "currency": req.currency,
                },
            )

            result = TransferResult(
                replay=False,
                source_customer_id=source.customer_id,
                data={
                    "transaction_id": tx_id, "idempotency_key": idempotency_key,
                    "status": "COMPLETED", "source_account": source.account_number,
                    "dest_account": dest.account_number, "amount": req.amount,
                    "currency": req.currency, "trace_id": trace_id,
                    "created_at": times["created_at"], "completed_at": times["completed_at"],
                },
            )
        # ---- Paso 9: COMMIT ocurre al salir del `async with session.begin()` ---------------
        # Todo o nada: saldos, transacción, asientos y eventos de outbox se confirman juntos.
        # Los bloqueos se liberan aquí; la siguiente transferencia en cola continúa.
        log.info(
            "transfer_completed",
            transaction_id=str(result.data["transaction_id"]),
            source_account=req.source_account, dest_account=req.dest_account,
            amount=str(req.amount), currency=req.currency,
        )
        return result
