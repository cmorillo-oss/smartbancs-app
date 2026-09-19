"""Demuestra POR QUÉ no se puede consultar el core legado en caliente, con números.

Parte 1: consultas directas a Bancs con concurrencia creciente -> latencia y tasa de 503.
Parte 2: enviar 1000 cambios de saldo ¿uno a uno o en lotes de 100?

Ataca a bancs-mock directamente (no pasa por la API). Usa cuentas ficticias (ACC-LOADTEST-*) para no
alterar el estado que la conciliación compara.
Uso:  make bancs-degradation   (o: python integration/bancs_degradation.py)
"""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

BANCS_URL = os.environ.get("BANCS_URL", "http://bancs-mock:8002")
OUT_DIR = Path(os.environ.get("EVIDENCE_BANCS_DIR", "/evidence/bancs"))
LEVELS = [1, 5, 10, 20, 40, 80]
REQUESTS_PER_LEVEL = 80


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def query_level(client: httpx.AsyncClient, concurrency: int) -> dict:
    sem = asyncio.Semaphore(concurrency)
    lat, codes = [], {}

    async def one():
        async with sem:
            t = time.perf_counter()
            r = await client.get("/bancs/v1/accounts/ACC-000001")
            lat.append((time.perf_counter() - t) * 1000)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(REQUESTS_PER_LEVEL)))
    wall = time.perf_counter() - started
    ok = codes.get(200, 0)
    return {"concurrency": concurrency, "requests": REQUESTS_PER_LEVEL, "p50_ms": round(pct(lat, 50)), "p95_ms": round(pct(lat, 95)),
            "http_503": codes.get(503, 0), "error_rate": round(codes.get(503, 0) / REQUESTS_PER_LEVEL, 3),
            "successful_per_second": round(ok / wall, 1)}


def event(seq: int) -> dict:
    return {"sequence": seq, "transaction_id": str(uuid.uuid4()), "source_account": f"ACC-LOADTEST-{seq % 50}",
            "dest_account": f"ACC-LOADTEST-{(seq + 1) % 50}", "amount": "1.00", "source_balance_after": "100.00", "dest_balance_after": "100.00"}


async def send(client, events) -> int:
    r = await client.post("/bancs/v1/accounts/balance/batch", json={"batch_id": str(uuid.uuid4()), "events": events})
    return r.status_code


async def batching_comparison(client: httpx.AsyncClient) -> dict:
    total = 1000
    base = 10**9  # secuencias enormes en cuentas ficticias: no interfieren con nada real
    # (a) un evento por petición, con la concurrencia que Bancs aguanta (10)
    sem = asyncio.Semaphore(10)
    codes = []

    async def single(i):
        async with sem:
            codes.append(await send(client, [event(base + i)]))

    t = time.perf_counter()
    await asyncio.gather(*(single(i) for i in range(total)))
    one_by_one = {"requests": total, "seconds": round(time.perf_counter() - t, 1), "http_503": codes.count(503),
                  "events_delivered": codes.count(200)}
    # (b) lotes de 100, secuenciales (como el worker)
    t = time.perf_counter()
    b_codes = [await send(client, [event(base + 10**6 + j * 100 + k) for k in range(100)]) for j in range(total // 100)]
    batched = {"requests": total // 100, "seconds": round(time.perf_counter() - t, 1), "http_503": b_codes.count(503),
               "events_delivered": b_codes.count(200) * 100}
    return {"one_event_per_request": one_by_one, "batches_of_100": batched}


async def main() -> int:
    async with httpx.AsyncClient(base_url=BANCS_URL, timeout=60.0, limits=httpx.Limits(max_connections=200, keepalive_expiry=2.0)) as client:
        levels = [await query_level(client, c) for c in LEVELS]
        await asyncio.sleep(2)
        batching = await batching_comparison(client)

    lines = ["BANCS BAJO CARGA: consultas directas (GET) con concurrencia creciente", "=" * 78,
             f"{'Simultáneas':>12}{'p50':>9}{'p95':>9}{'503':>7}{'% error':>10}{'OK/seg':>9}", "-" * 78]
    for lv in levels:
        lines.append(f"{lv['concurrency']:>12}{lv['p50_ms']:>7}ms{lv['p95_ms']:>7}ms{lv['http_503']:>7}{lv['error_rate']:>10.0%}{lv['successful_per_second']:>9}")
    o, b = batching["one_event_per_request"], batching["batches_of_100"]
    lines += ["", "ENVIAR 1000 CAMBIOS DE SALDO A BANCS", "=" * 78,
              f"  Uno por petición (10 simultáneas): {o['requests']:>5} peticiones  {o['seconds']:>6}s   503: {o['http_503']:>4}   entregados: {o['events_delivered']}",
              f"  Lotes de 100 (secuenciales):       {b['requests']:>5} peticiones  {b['seconds']:>6}s   503: {b['http_503']:>4}   entregados: {b['events_delivered']}",
              "", "Conclusión: por encima de 10 peticiones simultáneas Bancs se degrada; agrupar en lotes le presenta muchas menos peticiones."]
    text = "\n".join(lines)
    print(text)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "degradation.txt").write_text(text + "\n", encoding="utf-8")
    (OUT_DIR / "degradation.json").write_text(json.dumps({"query_levels": levels, "batching": batching}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
