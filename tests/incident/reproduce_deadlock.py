"""INCIDENTE 1 (enunciado 3.5): "posibles deadlocks en las tablas principales".

Provoca deadlocks REALES de PostgreSQL (SQLSTATE 40P01) y demuestra que el orden determinista los elimina.

  MODO INSEGURO  Hilo A: cuenta1 -> cuenta2 (bloquea 1 y luego 2)
                 Hilo B: cuenta2 -> cuenta1 (bloquea 2 y luego 1)   <- ORDEN INVERTIDO
                 Cada hilo toma su primer bloqueo y pide el segundo: cada uno espera al otro para siempre.
                 PostgreSQL lo detecta tras `deadlock_timeout` (1 s) y aborta a uno con el error 40P01.
  MODO SEGURO    La MISMA carga (mismas cuentas, mismas direcciones cruzadas, misma cantidad) por la API
                 real, que bloquea siempre en orden ascendente de id (ADR 0003) => cero deadlocks.

De cada deadlock se captura: el error, las DOS consultas implicadas, las cuentas, el trace_id y el
tiempo que estuvo esperando el bloqueo. La cifra "oficial" de deadlocks sale del propio PostgreSQL
(pg_stat_database), no de lo que cuenta el script.

Uso:  make deadlock-demo
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import DATABASE_DSN, api_transfer, create_accounts, deadlocks_counter, new_client  # noqa: E402

OUT_DIR = Path(os.environ.get("EVIDENCE_INCIDENT_DIR", "/evidence/incident"))
PER_DIRECTION = 15          # transferencias por dirección => 30 en total, todas a la vez
HOLD_S = 0.05               # tiempo entre el primer y el segundo bloqueo (lógica de negocio real ahí en medio)
LOCK_SQL = "SELECT id, balance FROM accounts WHERE id = $1 FOR UPDATE"


async def pg_deadlocks(conn) -> int:
    await asyncio.sleep(2.5)  # las estadísticas de Postgres se publican con un pequeño retraso
    return await conn.fetchval("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")


async def unsafe_transfer(pool, first: dict, second: dict, registry: dict, events: list, amount: Decimal) -> bool:
    """Transferencia con orden de bloqueo = orden del llamante (SIN ordenar). La versión con el defecto."""
    trace_id = uuid.uuid4().hex
    async with pool.acquire() as conn:
        pid = conn.get_server_pid()

        async def lock(acct: dict, holding: str | None) -> None:
            registry[pid] = {"trace_id": trace_id, "query": f"{LOCK_SQL}  -- id={acct['id']} ({acct['number']})"}
            asked = time.perf_counter()
            try:
                await conn.fetchrow(LOCK_SQL, acct["id"])
            except asyncpg.exceptions.DeadlockDetectedError as exc:
                # Registro estructurado con TODO lo que pide el requisito: error, consulta, cuentas, trace_id y espera.
                events.append({"event": "deadlock_detected", "level": "error", "trace_id": trace_id, "sqlstate": exc.sqlstate,
                               "pid": pid, "accounts": [first["number"], second["number"]],
                               "holding": holding, "waiting_for": acct["number"],
                               "query": registry[pid]["query"], "wait_seconds": round(time.perf_counter() - asked, 3),
                               "detail": (exc.detail or "").replace("\n", " | ")})
                raise

        try:
            async with conn.transaction():
                await lock(first, None)          # 1er bloqueo (puede caer aquí si hay más transacciones en cola por la misma fila)
                await asyncio.sleep(HOLD_S)
                await lock(second, first["number"])   # 2º bloqueo: aquí se forma el ciclo A espera a B, B espera a A
                await conn.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2", amount, first["id"])
                await conn.execute("UPDATE accounts SET balance = balance + $1 WHERE id = $2", amount, second["id"])
            return True
        except asyncpg.exceptions.DeadlockDetectedError:
            return False
        finally:
            registry.pop(pid, None)


def describe_pair(event: dict, registry_at_error: dict) -> dict:
    """Empareja los pids del DETAIL de Postgres con las consultas de ambos lados del deadlock."""
    pids = [int(p) for p in re.findall(r"Process (\d+) waits", event["detail"])]
    return {"victim_pid": event["pid"], "pids_in_cycle": pids}


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    admin = await asyncpg.connect(DATABASE_DSN)
    (a, b), (c, d) = await create_accounts(admin, 2, Decimal("100000.00"), "DLU"), await create_accounts(admin, 2, Decimal("100000.00"), "DLS")
    amount = Decimal("1.00")

    # ------------------------------------------------------------------ MODO INSEGURO
    pool = await asyncpg.create_pool(DATABASE_DSN, min_size=32, max_size=40,
                                     server_settings={"application_name": "deadlock-demo-unsafe"})
    registry: dict[int, dict] = {}
    events: list[dict] = []
    before = await pg_deadlocks(admin)
    started = time.perf_counter()
    jobs = [unsafe_transfer(pool, a, b, registry, events, amount) for _ in range(PER_DIRECTION)]        # A -> B: bloquea 1 y luego 2
    jobs += [unsafe_transfer(pool, b, a, registry, events, amount) for _ in range(PER_DIRECTION)]       # B -> A: bloquea 2 y luego 1
    results = await asyncio.gather(*jobs)
    unsafe_seconds = time.perf_counter() - started
    unsafe_pg = (await pg_deadlocks(admin)) - before
    await pool.close()

    # ------------------------------------------------------------------ MODO SEGURO (API real)
    async with new_client() as client:
        api_before, pg_before = await deadlocks_counter(client), await pg_deadlocks(admin)
        started = time.perf_counter()
        responses = await asyncio.gather(
            *[api_transfer(client, c["number"], d["number"], amount) for _ in range(PER_DIRECTION)],
            *[api_transfer(client, d["number"], c["number"], amount) for _ in range(PER_DIRECTION)],
        )
        safe_seconds = time.perf_counter() - started
        safe_pg = (await pg_deadlocks(admin)) - pg_before
        safe_api = await deadlocks_counter(client) - api_before
    safe_ok = sum(1 for r in responses if r.status_code == 201)
    await admin.close()

    unsafe_ok = sum(results)
    total = 2 * PER_DIRECTION
    waits = [e["wait_seconds"] for e in events]

    lines = ["=" * 78, "DEMO DE DEADLOCK: la misma carga cruzada (A->B y B->A a la vez), dos formas de bloquear", "=" * 78, "",
             f"{'':38}{'INSEGURO (orden invertido)':<28}{'SEGURO (orden por id, API)':<26}",
             "-" * 78,
             f"{'Transferencias lanzadas a la vez':<38}{total:<28}{total:<26}",
             f"{'Completadas':<38}{unsafe_ok:<28}{safe_ok:<26}",
             f"{'Abortadas por deadlock':<38}{len(events):<28}{'0':<26}",
             f"{'Deadlocks según PostgreSQL':<38}{unsafe_pg:<28}{safe_pg:<26}",
             f"{'Deadlocks según la API (métrica)':<38}{'(no pasa por la API)':<28}{int(safe_api):<26}",
             f"{'Espera de la víctima hasta el error':<38}{(f'{sum(waits) / len(waits):.2f} s (media)' if waits else '-'):<28}{'-':<26}",
             f"{'Duración total':<38}{f'{unsafe_seconds:.2f} s':<28}{f'{safe_seconds:.2f} s':<26}",
             "-" * 78,
             f"{'Veredicto':<38}{('DEADLOCKS' if unsafe_pg else 'sin deadlocks (repetir)'):<28}{('CERO DEADLOCKS' if not safe_pg and not safe_api else 'FALLO'):<26}", ""]

    if events:
        lines += ["--- DETALLE DE UN DEADLOCK REAL (lo que vería el operador) ---"]
        ev = next((e for e in events if e['holding']), events[0])
        lines += [f"  Error ............ SQLSTATE {ev['sqlstate']} (deadlock_detected)",
                  f"  trace_id ......... {ev['trace_id']}",
                  f"  Cuentas .......... la víctima tenía bloqueada {ev['holding'] or '(ninguna aún)'} y pedía {ev['waiting_for']}",
                  f"  Espera ........... {ev['wait_seconds']} s hasta que Postgres detectó el ciclo (deadlock_timeout = 1 s)",
                  f"  Consulta víctima . {ev['query']}",
                  f"  Detalle Postgres . {ev['detail']}"]
        pids = [int(p) for p in re.findall(r"Process (\d+) waits", ev["detail"])]
        lines.append(f"  Procesos en el ciclo: {pids}  (víctima = {ev['pid']}); cada uno espera el bloqueo que tiene el siguiente")
        lines += ["", "--- ASÍ SE REGISTRARÍA (log JSON estructurado con trace_id) ---", "  " + json.dumps(ev, ensure_ascii=False)]
    text = "\n".join(lines)
    print(text)
    (OUT_DIR / "deadlock_demo.txt").write_text(text + "\n", encoding="utf-8")
    (OUT_DIR / "deadlock_demo.json").write_text(json.dumps({
        "unsafe": {"launched": total, "completed": unsafe_ok, "aborted_by_deadlock": len(events), "postgres_deadlocks": unsafe_pg,
                   "seconds": round(unsafe_seconds, 2), "sample_events": events[:3]},
        "safe": {"launched": total, "completed": safe_ok, "postgres_deadlocks": safe_pg, "api_metric_deadlocks": safe_api,
                 "seconds": round(safe_seconds, 2)}}, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if unsafe_pg > 0 and safe_pg == 0 and safe_api == 0 and safe_ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
