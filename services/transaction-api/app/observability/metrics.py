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

# Estado inicial explícito del breaker: sin esto, el gauge arrancaría en 0 igualmente,
# pero declararlo deja claro que "0 = cerrado = sano" es el valor por defecto.
AI_CIRCUIT_BREAKER_STATE.set(0)


def register_pool_gauge(engine) -> None:
    """Expone el estado del pool SIN un hilo de sondeo.

    POR QUÉ set_function: Prometheus llama a la función en cada scrape, así que el valor es
    siempre el del instante de la lectura y no gastamos CPU entre scrapes.
    Nota: "waiting" (peticiones en cola esperando conexión) no lo expone SQLAlchemy;
    se añadirá en la Fase 7, cuando se reproduzca el agotamiento del pool.
    """
    pool = engine.sync_engine.pool
    DB_POOL_CONNECTIONS.labels(state="size").set_function(lambda: pool.size())
    DB_POOL_CONNECTIONS.labels(state="checked_in").set_function(lambda: pool.checkedin())
    DB_POOL_CONNECTIONS.labels(state="checked_out").set_function(lambda: pool.checkedout())
    # overflow() es negativo mientras no se usa el overflow (parte de -pool_size); lo acotamos a 0.
    DB_POOL_CONNECTIONS.labels(state="overflow").set_function(lambda: max(0, pool.overflow()))


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
