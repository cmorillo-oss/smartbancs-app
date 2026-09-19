"""INCIDENTE 2 (enunciado 3.5): "timeouts de conexión con la BD" por agotamiento del pool.

Cómo se reproduce (así ocurre en la vida real): UNA transacción larga retiene el bloqueo de una fila
caliente. Las transferencias que la necesitan esperan el bloqueo SIN soltar su conexión; en pocos
segundos hay más peticiones que conexiones y el pool se agota: las demás peticiones ni siquiera
consiguen conexión y fallan por timeout. Una causa pequeña (1 sesión) paraliza todo.

Este script:
  1. mide el estado sano (pool, /ready);
  2. simula al "culpable": una sesión que abre transacción, bloquea una cuenta y se queda ahí;
  3. lanza tráfico de transferencias (incluye las de esa cuenta) durante DURATION segundos;
  4. MIENTRAS DURA el incidente muestrea: métrica pool{state="waiting"}, /ready y los endpoints de
     diagnóstico, y comprueba que identifican al culpable (¿en cuántos segundos?);
  5. consulta a Prometheus qué alertas dispararon;
  6. libera el bloqueo y verifica la recuperación.

Uso:  make exhaust-pool
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
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import API_URL, DATABASE_DSN, create_accounts  # noqa: E402

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
OUT_DIR = Path(os.environ.get("EVIDENCE_INCIDENT_DIR", "/evidence/incident"))
DURATION = 45        # segundos de incidente (las alertas necesitan tiempo sostenido para pasar de pending a firing)
BURST = 25           # transferencias nuevas cada 0.5 s (~50/s). Cada una retiene su conexión 3 s (lock_timeout)
                     # => ~150 conexiones demandadas frente a un pool de 30: saturación segura


async def metric(client: httpx.AsyncClient, name_regex: str) -> float | None:
    try:
        text = (await client.get("/metrics")).text
    except httpx.TransportError:  # una conexión keep-alive que el servidor ya cerró: se reintenta una vez con otra nueva
        text = (await client.get("/metrics")).text
    m = re.search(name_regex + r" (\S+)", text, re.M)
    return float(m.group(1)) if m else None


async def main() -> int:
    log: list[str] = []

    def say(msg: str = "") -> None:
        print(msg, flush=True)
        log.append(msg)

    result: dict = {"samples": []}
    # DOS clientes HTTP independientes: el de la CARGA (cientos de peticiones colgadas esperando) y el del
    # OPERADOR (sondeo y diagnóstico). Con uno solo, las peticiones del operador harían cola detrás de las
    # de carga en el pool del PROPIO cliente y el script se bloquearía a sí mismo (error de diseño que
    # cometí en la primera versión). En la realidad el operador tiene su propia conexión, como aquí.
    async with httpx.AsyncClient(base_url=API_URL, timeout=15.0, limits=httpx.Limits(max_connections=20, keepalive_expiry=2.0)) as api, \
            httpx.AsyncClient(base_url=API_URL, timeout=30.0, limits=httpx.Limits(max_connections=250, keepalive_expiry=2.0)) as loadc, \
            httpx.AsyncClient(timeout=10.0) as prom:
        db = await asyncpg.connect(DATABASE_DSN, server_settings={"application_name": "incident-simulator"})
        hot, other = await create_accounts(db, 2, Decimal("100000.00"), prefix="INC")
        pool_capacity = int(await metric(api, r'^smartbancs_db_pool_connections\{state="capacity"\}') or 0)

        # Pre-vuelo: esperar a que no haya alertas activas de corridas anteriores (sus ventanas de 1 min
        # hacen que sigan "firing" un rato) para poder atribuir cada alerta A ESTE incidente.
        for _ in range(60):
            try:
                active = [a for a in (await prom.get(f"{PROM_URL}/api/v1/alerts")).json()["data"]["alerts"] if a["state"] == "firing"]
            except Exception:  # noqa: BLE001
                active = []
            if not active:
                break
            await asyncio.sleep(3)

        say("=" * 78)
        say("INCIDENTE: agotamiento del pool por una transacción larga que bloquea una fila caliente")
        say("=" * 78)
        r = await api.get("/ready")
        say(f"[t=0] Estado sano: /ready -> HTTP {r.status_code} {r.json()} | capacidad del pool: {pool_capacity} conexiones")

        # ---- 2) el culpable: abre transacción, bloquea la cuenta caliente y NO la cierra ----
        culprit = await asyncpg.connect(DATABASE_DSN, server_settings={"application_name": "incident-simulator"})
        culprit_pid = culprit.get_server_pid()   # se lee ANTES de bloquear: que su última consulta sea la del bloqueo
        culprit_tx = culprit.transaction()
        await culprit_tx.start()
        await culprit.execute("SELECT 1 FROM accounts WHERE id = $1 FOR UPDATE", hot["id"])
        say(f"[t=0] Sesión culpable (pid {culprit_pid}) bloquea la cuenta {hot['number']} y no termina su transacción.")

        # ---- 3) carga: todas las transferencias tocan la cuenta caliente ----
        stop = asyncio.Event()
        outcomes: dict[str, int] = {}

        async def one():
            try:
                resp = await loadc.post("/api/v1/transactions", headers={"Idempotency-Key": str(uuid.uuid4())},
                                      json={"source_account": other["number"], "dest_account": hot["number"], "amount": "1.00", "currency": "USD"})
                key = str(resp.status_code) + (":" + resp.json().get("error_code", "") if resp.status_code >= 400 else "")
            except Exception as exc:  # noqa: BLE001 - el cliente también puede fallar bajo carga; se contabiliza
                key = f"cliente:{type(exc).__name__}"
            outcomes[key] = outcomes.get(key, 0) + 1

        async def load():
            tasks = []
            while not stop.is_set():
                tasks += [asyncio.create_task(one()) for _ in range(BURST)]
                await asyncio.sleep(0.5)
            await asyncio.gather(*tasks)

        loader = asyncio.create_task(load())

        # ---- 4) muestreo durante el incidente ----
        t0 = time.monotonic()
        peak_waiting, first_not_ready, culprit_found_at, diag = 0.0, None, None, {}
        alert_seen: dict[str, dict] = {}   # alerta -> {'pending': t, 'firing': t}
        while time.monotonic() - t0 < DURATION:
            await asyncio.sleep(1.0)
            t = round(time.monotonic() - t0, 1)
            waiting = await metric(api, r'^smartbancs_db_pool_connections\{state="waiting"\}') or 0
            used = await metric(api, r'^smartbancs_db_pool_connections\{state="checked_out"\}') or 0
            peak_waiting = max(peak_waiting, waiting)
            rt0 = time.monotonic()
            rd = await api.get("/ready")
            ready_ms = round((time.monotonic() - rt0) * 1000)
            if rd.status_code != 200 and first_not_ready is None:
                first_not_ready = t
            sample = {"t": t, "pool_checked_out": used, "pool_waiting": waiting, "ready_http": rd.status_code, "ready_ms": ready_ms,
                      "ready_database": rd.json().get("database")}
            # Diagnóstico: ¿cuánto tarda el operador en identificar al culpable?
            if culprit_found_at is None and t >= 1:
                d0 = time.monotonic()
                tree = (await api.get("/api/v1/admin/diagnostics/blocking-tree", timeout=10)).json()
                if tree.get("tree"):
                    culprit_found_at = t
                    diag = {"blocking_tree": tree, "pool": (await api.get("/api/v1/admin/diagnostics/pool")).json(),
                            "locks": (await api.get("/api/v1/admin/diagnostics/locks")).json(),
                            "diagnostic_query_ms": round((time.monotonic() - d0) * 1000)}
            try:
                for al in (await prom.get(f"{PROM_URL}/api/v1/alerts")).json()["data"]["alerts"]:
                    alert_seen.setdefault(al["labels"]["alertname"], {}).setdefault(al["state"], t)
            except Exception:  # noqa: BLE001 - Prometheus no debe romper la demo
                pass
            result["samples"].append(sample)
            say(f"[t={t:>4}s] pool en uso {used:>4.0f}/{pool_capacity} | esperando conexión {waiting:>4.0f} | /ready -> HTTP {rd.status_code} ({sample['ready_database']}) en {ready_ms} ms")

        alerts = alert_seen

        # ---- 6) recuperación ----
        stop.set()
        await loader
        await culprit_tx.rollback()
        await culprit.close()
        rec0 = time.monotonic()
        recovered_after = None
        while time.monotonic() - rec0 < 30:
            rd = await api.get("/ready")
            w = await metric(api, r'^smartbancs_db_pool_connections\{state="waiting"\}') or 0
            if rd.status_code == 200 and w == 0:
                recovered_after = round(time.monotonic() - rec0, 1)
                break
            await asyncio.sleep(0.5)
        await db.close()

    say("")
    say("--- QUÉ VIO EL CLIENTE durante el incidente (resultados de las transferencias) ---")
    total = sum(outcomes.values())
    for k, v in sorted(outcomes.items(), key=lambda x: -x[1]):
        say(f"  {v:>6}  ({v / total:>5.1%})  {k}")
    say("")
    say("--- DETECCIÓN ---")
    say(f"  Pico de peticiones esperando conexión (métrica pool{{state=\"waiting\"}}): {peak_waiting:.0f}")
    say(f"  /ready detectó la degradación (HTTP 503) a los {first_not_ready} s" if first_not_ready is not None else "  /ready NO detectó degradación")
    if culprit_found_at is not None:
        say(f"  Endpoint /diagnostics/blocking-tree identificó al culpable a los {culprit_found_at} s (consulta: {diag['diagnostic_query_ms']} ms)")
        say(f"    -> {diag['blocking_tree']['summary']}")
        say(f"  Endpoint /diagnostics/pool: {diag['pool']['verdict']}")
    else:
        say("  /diagnostics/blocking-tree NO encontró al culpable")
    say("  Alertas de Prometheus (segundo del incidente en que pasaron a pending / firing):")
    for name, st in sorted(alerts.items()):
        say(f"    {name:<28} pending a los {st.get('pending', '-')} s  |  firing a los {st.get('firing', '-')} s")
    if not alerts:
        say("    (ninguna)")
    say("")
    say("--- RECUPERACIÓN ---")
    say(f"  Al liberar el bloqueo, el sistema volvió a estado sano en {recovered_after} s (/ready = 200 y sin peticiones en cola)"
        if recovered_after is not None else "  NO se recuperó en 30 s")

    result.update({
        "duration_s": DURATION, "pool_capacity": pool_capacity, "client_outcomes": outcomes, "peak_waiting": peak_waiting,
        "ready_first_503_at_s": first_not_ready, "culprit_identified_at_s": culprit_found_at, "diagnostics": diag,
        "prometheus_alerts": alerts, "recovered_after_s": recovered_after,
    })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "exhaust_pool.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    (OUT_DIR / "exhaust_pool.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    ok = first_not_ready is not None and culprit_found_at is not None and recovered_after is not None and peak_waiting > 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
