"""Métricas Prometheus (nombres exactos definidos en el brief).

Método RED: Rate (transactions_total), Errors (transaction_errors_total),
Duration (transaction_duration_seconds). Más las métricas USE del recurso crítico
(pool de BD, bloqueos) y de las dependencias (IA, Bancs, outbox).
"""
import time

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import event

# Buckets alineados con el SLO: 2s es el límite del reto, así que hay un bucket exacto en 2
# y buckets finos por debajo (donde debería vivir el p95) para que el cálculo sea preciso.
_TX_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)

# --- RED del negocio ---
TRANSACTIONS_TOTAL = Counter(
    "smartbancs_transactions_total", "Transacciones procesadas", ["status", "currency"]
)
TRANSACTION_DURATION = Histogram(
    "smartbancs_transaction_duration_seconds",
    "Duración de operaciones transaccionales",
    ["operation"],
    buckets=_TX_BUCKETS,
)
# error_code es de cardinalidad baja y acotada (INSUFFICIENT_FUNDS, LOCK_TIMEOUT...):
# nunca poner ids de usuario/cuenta como label, o Prometheus explota en memoria.
TRANSACTION_ERRORS = Counter(
    "smartbancs_transaction_errors_total", "Errores de transacción por código", ["error_code"]
)

# --- Base de datos: el cuello de botella esperado ---
DB_QUERY_DURATION = Histogram(
    "smartbancs_db_query_duration_seconds",
    "Duración de consultas SQL",
    ["operation"],
    buckets=_TX_BUCKETS,
)
DB_DEADLOCKS = Counter("smartbancs_db_deadlocks_total", "Deadlocks detectados por PostgreSQL (40P01)")
DB_LOCK_WAIT = Histogram(
    "smartbancs_db_lock_wait_seconds",
    "Tiempo esperando bloqueos de fila (SELECT ... FOR UPDATE)",
    buckets=_TX_BUCKETS,
)
DB_POOL_CONNECTIONS = Gauge(
    "smartbancs_db_pool_connections", "Conexiones del pool por estado", ["state"]
)

# --- IA: dependencia que NUNCA debe afectar al camino crítico ---
AI_CALLS_TOTAL = Counter("smartbancs_ai_calls_total", "Llamadas al servicio de IA", ["status"])
AI_CALL_DURATION = Histogram(
    "smartbancs_ai_call_duration_seconds", "Latencia de llamadas a la IA", buckets=_TX_BUCKETS
)
# Codificación del estado: 0=CLOSED, 1=OPEN, 2=HALF_OPEN (un gauge solo guarda números).
AI_CIRCUIT_BREAKER_STATE = Gauge(
    "smartbancs_ai_circuit_breaker_state", "Estado del circuit breaker de IA (0=CLOSED,1=OPEN,2=HALF_OPEN)"
)

# --- Outbox / Bancs ---
OUTBOX_PENDING = Gauge("smartbancs_outbox_pending_events", "Eventos del outbox pendientes de enviar")
BANCS_SYNC_TOTAL = Counter("smartbancs_bancs_sync_total", "Lotes sincronizados con Bancs", ["status"])

BANCS_CIRCUIT_BREAKER_STATE = Gauge(
    "smartbancs_bancs_circuit_breaker_state", "Estado del circuit breaker de Bancs (0=CLOSED,1=OPEN,2=HALF_OPEN)"
)
BANCS_SYNC_BATCH_SIZE = Histogram(
    "smartbancs_bancs_sync_batch_events", "Eventos por lote enviado a Bancs", buckets=(1, 5, 10, 25, 50, 75, 100)
)
BANCS_SYNC_DURATION = Histogram(
    "smartbancs_bancs_sync_duration_seconds", "Latencia de un lote hacia Bancs", buckets=_TX_BUCKETS
)

# Estado inicial explícito del breaker: sin esto, el gauge arrancaría en 0 igualmente,
# pero declararlo deja claro que "0 = cerrado = sano" es el valor por defecto.
AI_CIRCUIT_BREAKER_STATE.set(0)


# Peticiones que AHORA MISMO esperan una conexión del pool (ver install_pool_waiter_tracking).
_pool_waiters = 0


def pool_waiting() -> int:
    return _pool_waiters


def pool_snapshot(engine) -> dict:
    """Estado del pool en este instante (lo usan la métrica, /ready y el endpoint de diagnóstico)."""
    pool = engine.sync_engine.pool
    size, overflow = pool.size(), max(0, pool.overflow())
    capacity = size + pool._max_overflow  # tope real de conexiones: pool_size + max_overflow
    out = pool.checkedout()
    return {"size": size, "checked_in": pool.checkedin(), "checked_out": out, "overflow": overflow,
            "capacity": capacity, "waiting": _pool_waiters, "utilization": round(out / capacity, 3) if capacity else 0.0,
            "pool_timeout_s": pool._timeout}


def install_pool_waiter_tracking(engine) -> None:
    """Cuenta cuántas peticiones están BLOQUEADAS esperando una conexión libre.

    SQLAlchemy no expone este dato, pero es LA señal del agotamiento del pool: con "checked_out" al
    máximo solo sabemos que el pool está lleno, no cuánta gente hace cola detrás. Envolvemos el punto
    donde el pool entrega (o hace esperar por) una conexión. Si hay conexión libre el contador sube y
    baja en microsegundos; bajo saturación las peticiones se quedan ahí hasta pool_timeout, y eso es
    lo que se ve en la métrica.
    """
    pool = engine.sync_engine.pool
    original = pool._do_get

    def tracked():
        global _pool_waiters
        _pool_waiters += 1
        try:
            return original()
        finally:
            _pool_waiters -= 1

    pool._do_get = tracked


def register_pool_gauge(engine) -> None:
    """Expone el estado del pool SIN un hilo de sondeo.

    POR QUÉ set_function: Prometheus llama a la función en cada scrape, así que el valor es
    siempre el del instante de la lectura y no gastamos CPU entre scrapes.
    """
    install_pool_waiter_tracking(engine)
    for state in ("size", "checked_in", "checked_out", "overflow", "waiting", "capacity"):
        DB_POOL_CONNECTIONS.labels(state=state).set_function(lambda st=state: pool_snapshot(engine)[st])


_KNOWN_OPS = {"select", "insert", "update", "delete", "begin", "commit", "rollback", "set"}


def register_query_timing(engine) -> None:
    """Mide cada sentencia SQL con eventos de SQLAlchemy.

    POR QUÉ eventos y no cronometrar a mano en cada repositorio: cubre TODAS las consultas
    (incluidas las futuras) sin depender de que alguien se acuerde de instrumentarlas.
    """

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("query_start", []).append(time.perf_counter())

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        elapsed = time.perf_counter() - conn.info["query_start"].pop()
        first_word = statement.lstrip().split(None, 1)[0].lower() if statement.strip() else "other"
        # Label acotado a un conjunto fijo: el SQL completo como label crearía cardinalidad infinita.
        DB_QUERY_DURATION.labels(operation=first_word if first_word in _KNOWN_OPS else "other").observe(elapsed)
