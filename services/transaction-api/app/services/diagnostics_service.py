"""Consultas de diagnóstico: identificar el cuello de botella EN SEGUNDOS (requisito literal de 3.5).

Todas usan el pool de diagnóstico (2 conexiones reservadas, ver database.py) para seguir funcionando
justo cuando el pool principal está agotado.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine
from app.observability.metrics import pool_snapshot
from app.services.blocking_tree import build_tree


async def slow_queries(session: AsyncSession, limit: int) -> dict:
    """Las consultas que MÁS tiempo total consumen, según pg_stat_statements.

    Se ordena por tiempo TOTAL (no por el máximo de una ejecución): una consulta de 5 ms ejecutada
    un millón de veces pesa más en la BD que una de 2 s ejecutada una vez.
    """
    ext = await session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"))
    if ext.first() is None:
        return {"available": False, "reason": "extensión pg_stat_statements no instalada", "queries": []}
    rows = await session.execute(
        text(
            """
            SELECT queryid::text AS query_id, calls,
                   round(total_exec_time::numeric, 1) AS total_ms,
                   round(mean_exec_time::numeric, 2)  AS mean_ms,
                   round(max_exec_time::numeric, 1)   AS max_ms,
                   rows, left(regexp_replace(query, '\\s+', ' ', 'g'), 240) AS query
              FROM pg_stat_statements
             WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND query NOT ILIKE '%pg_stat_statements%' AND query NOT ILIKE '%pg_stat_activity%'
               -- Se excluyen DDL, cargas y utilidades (creación del esquema, COPY del ETL...): no son tráfico de la aplicación
               AND query !~* '^[[:space:]]*(create|alter|drop|truncate|copy|vacuum|analyze|set|show|begin|commit|rollback)'
             ORDER BY total_exec_time DESC
             LIMIT :lim
            """
        ),
        {"lim": limit},
    )
    return {"available": True, "ordered_by": "total_exec_time", "queries": [dict(r) for r in rows.mappings()]}


async def _blocking_sessions(session: AsyncSession) -> list[dict]:
    """Sesiones implicadas en un bloqueo: las que esperan Y las que bloquean, con quién bloquea a quién."""
    rows = await session.execute(
        text(
            """
            WITH blocked AS (
                SELECT a.pid, pg_blocking_pids(a.pid) AS blockers
                  FROM pg_stat_activity a
                 WHERE a.datname = current_database() AND cardinality(pg_blocking_pids(a.pid)) > 0
            ), involved AS (
                SELECT pid FROM blocked UNION SELECT unnest(blockers) FROM blocked
            )
            SELECT a.pid, a.application_name, a.state, a.wait_event_type, a.wait_event,
                   round(extract(epoch FROM now() - a.xact_start)::numeric, 2)  AS transaction_seconds,
                   round(extract(epoch FROM now() - a.query_start)::numeric, 2) AS query_seconds,
                   left(regexp_replace(a.query, '\\s+', ' ', 'g'), 300) AS query,
                   COALESCE((SELECT b.blockers FROM blocked b WHERE b.pid = a.pid), '{}') AS blocked_by
              FROM pg_stat_activity a JOIN involved i ON i.pid = a.pid
             ORDER BY a.xact_start NULLS LAST
             LIMIT 500  -- tope defensivo: un incidente enorme no debe convertir el diagnóstico en otro problema
            """
        )
    )
    sessions = [dict(r) for r in rows.mappings()]
    if sessions:
        locks = await session.execute(
            text(
                """
                SELECT l.pid, l.locktype, l.mode, l.granted, c.relname AS relation
                  FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation
                 WHERE l.pid = ANY(:pids) AND l.locktype IN ('relation', 'tuple', 'transactionid')
                   AND (c.relname IS NULL OR c.relname NOT LIKE 'pg_%')
                """
            ),
            {"pids": [s["pid"] for s in sessions]},
        )
        by_pid: dict[int, list] = {}
        for lk in locks.mappings():
            by_pid.setdefault(lk["pid"], []).append(
                {"type": lk["locktype"], "mode": lk["mode"], "granted": lk["granted"], "relation": lk["relation"]})
        for s in sessions:
            s["locks"] = by_pid.get(s["pid"], [])
    return sessions


async def locks(session: AsyncSession) -> dict:
    sessions = await _blocking_sessions(session)
    waiting = [s for s in sessions if s["blocked_by"]]
    return {"blocked_sessions": len(waiting), "sessions": sessions}


async def blocking_tree(session: AsyncSession) -> dict:
    """Árbol de bloqueos: cada raíz es una sesión que bloquea a otras SIN estar bloqueada ella misma
    (el culpable); sus hijos, las sesiones que frena. Responde "¿a quién mato para destrabar todo?".
    La construcción está en blocking_tree.py (función pura, con su test: ver la lección aprendida allí)."""
    return build_tree(await _blocking_sessions(session))


async def pool(session: AsyncSession | None) -> dict:
    """Estado del pool de la API + el reverso en la BD (quién tiene conexiones abiertas y en qué estado)."""
    out = {"api_pool": pool_snapshot(engine)}
    if session is not None:
        try:
            rows = await session.execute(
                text(
                    """
                    SELECT COALESCE(NULLIF(application_name, ''), '(sin nombre)') AS application_name, state, count(*) AS connections
                      FROM pg_stat_activity WHERE datname = current_database()
                     GROUP BY 1, 2 ORDER BY 3 DESC
                    """
                )
            )
            out["database_connections"] = [dict(r) for r in rows.mappings()]
            out["max_connections"] = int((await session.execute(text("SHOW max_connections"))).scalar_one())
        except Exception as exc:  # noqa: BLE001 - el estado del pool de la API debe salir aunque la BD falle
            out["database_error"] = type(exc).__name__
    snap = out["api_pool"]
    # Veredicto en una línea: lo primero que lee un operador a las 3 de la mañana.
    if snap["waiting"] > 0:
        out["verdict"] = f"POOL AGOTADO: {snap['waiting']} peticiones esperando conexión ({snap['checked_out']}/{snap['capacity']} en uso)"
    elif snap["utilization"] >= 0.8:
        out["verdict"] = f"POOL AL {snap['utilization']:.0%}: cerca del agotamiento"
    else:
        out["verdict"] = "pool sano"
    return out
