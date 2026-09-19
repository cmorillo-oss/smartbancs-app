"""Prueba de carga: el pico de quincena.

  * Rampa PROGRESIVA por escalones hasta saturar: en cada escalón se mide el rendimiento REAL; el TPS
    máximo sostenido es el del mejor escalón que aún cumple el SLO (p95 < 2 s y errores < 1%).
  * Mezcla realista: 70% transferencias, 20% consulta de saldo, 10% historial.
  * Transferencias REPARTIDAS entre MUCHAS cuentas (LT_ACCOUNTS, por defecto 5000), elegidas al azar y
    distintas entre sí: mide el rendimiento del sistema, no la contención de una fila caliente. (Sobre UNA
    cuenta el límite es el bloqueo de esa fila: ~30 TPS, medido en la Fase 4, y no dice nada de la capacidad.)
  * Bucle cerrado sin tiempo de espera entre peticiones: cada usuario virtual = 1 petición en vuelo, así
    que "usuarios" equivale a concurrencia y el TPS que sale es la capacidad, no la oferta.

Uso:  make load     (orquesta: crea cuentas, mide CPU de los contenedores, ejecuta esto y verifica el dinero)
Los resultados se guardan en evidence/load-test-results/.
"""
import json
import os
import random
import re
import time
import uuid
from pathlib import Path

# locust se importa PRIMERO: parchea la biblioteca estándar para gevent; `requests` debe cargarse después.
from locust import HttpUser, LoadTestShape, constant, events, task  # isort: skip

import gevent  # noqa: E402
import requests  # noqa: E402

N_ACCOUNTS = int(os.environ.get("LT_ACCOUNTS", "5000"))
OUT_DIR = Path(os.environ.get("EVIDENCE_LOAD_DIR", "/evidence/load-test-results"))
SLO_P95_MS = 2000
SLO_ERROR_RATE = 0.01
SETTLE_S = 6          # segundos iniciales de cada escalón que NO se miden (se estabiliza la concurrencia y el pool)

# (usuarios simultáneos, duración del escalón en segundos)
STAGES = [(10, 25), (25, 25), (50, 25), (75, 25), (100, 25), (150, 25), (200, 25), (300, 25)]

_records: list[tuple[float, str, float, bool]] = []   # (t relativo, nombre, ms, ok)
_pool_samples: list[dict] = []
_t0 = 0.0


def acct(i: int) -> str:
    return f"LT-{i:05d}"


class BankUser(HttpUser):
    wait_time = constant(0)

    @task(70)
    def transfer(self):
        a, b = random.sample(range(1, N_ACCOUNTS + 1), 2)   # dos cuentas DISTINTAS al azar entre miles
        with self.client.post(
            "/api/v1/transactions", name="POST /transactions",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"source_account": acct(a), "dest_account": acct(b), "amount": str(random.randint(1, 50)), "currency": "USD"},
            catch_response=True,
        ) as r:
            if r.status_code == 201:
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}: {r.text[:80]}")

    @task(20)
    def balance(self):
        self.client.get(f"/api/v1/accounts/{acct(random.randint(1, N_ACCOUNTS))}", name="GET /accounts/[n]")

    @task(10)
    def history(self):
        self.client.get(f"/api/v1/accounts/{acct(random.randint(1, N_ACCOUNTS))}/transactions?limit=20", name="GET /accounts/[n]/transactions")


class PeakShape(LoadTestShape):
    """Escalones de concurrencia creciente hasta saturación."""

    def tick(self):
        t = self.get_run_time()
        end = 0
        for users, seconds in STAGES:
            end += seconds
            if t < end:
                return users, 1000   # spawn_rate alto: el escalón alcanza su concurrencia casi de inmediato
        return None


@events.test_start.add_listener
def on_start(environment, **kw):
    global _t0
    _t0 = time.time()
    _records.clear()
    _pool_samples.clear()
    gevent.spawn(_sample_pool, environment.host)


def _sample_pool(host: str) -> None:
    """Cada 2 s lee de /metrics el estado del pool de la API: es lo que dice si el cuello de botella es la BD."""
    while True:
        try:
            text = requests.get(f"{host}/metrics", timeout=5).text
            get = lambda st: float(re.search(rf'smartbancs_db_pool_connections{{state="{st}"}} (\S+)', text).group(1))
            _pool_samples.append({"t": time.time() - _t0, "waiting": get("waiting"), "checked_out": get("checked_out")})
        except Exception:  # noqa: BLE001 - el muestreo nunca debe interferir con la carga
            pass
        gevent.sleep(2)


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, exception, **kw):
    _records.append((time.time() - _t0, name, response_time, exception is None))


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@events.test_stop.add_listener
def on_stop(environment, **kw):
    stages, start = [], 0
    for users, seconds in STAGES:
        lo, hi = start + SETTLE_S, start + seconds
        win = hi - lo
        rows = [r for r in _records if lo <= r[0] < hi]
        tr = [r for r in rows if r[1] == "POST /transactions"]
        ok_tr = [r for r in tr if r[3]]
        pool = [p for p in _pool_samples if lo <= p["t"] < hi]
        if rows and tr:
            lat = [r[2] for r in tr]
            stages.append({
                "users": users, "measured_seconds": win,
                "requests_per_second": round(len(rows) / win, 1),
                "transfers_per_second": round(len(ok_tr) / win, 1),
                "transfer_p50_ms": round(pct(lat, 50)), "transfer_p95_ms": round(pct(lat, 95)), "transfer_p99_ms": round(pct(lat, 99)),
                "all_requests_p95_ms": round(pct([r[2] for r in rows], 95)),
                "error_rate": round(sum(1 for r in rows if not r[3]) / len(rows), 4),
                "transfers": len(tr), "transfer_errors": len(tr) - len(ok_tr),
                "pool_waiting_max": max((p["waiting"] for p in pool), default=None),
                "pool_checked_out_avg": round(sum(p["checked_out"] for p in pool) / len(pool), 1) if pool else None,
            })
        start += seconds

    good = [s for s in stages if s["transfer_p95_ms"] < SLO_P95_MS and s["error_rate"] < SLO_ERROR_RATE]
    best = max(good, key=lambda s: s["requests_per_second"]) if good else None
    peak = max(stages, key=lambda s: s["requests_per_second"]) if stages else None
    result = {"accounts": N_ACCOUNTS, "mix": "70% transferencias / 20% saldo / 10% historial", "settle_seconds_excluded": SETTLE_S,
              "slo": {"p95_ms": SLO_P95_MS, "error_rate": SLO_ERROR_RATE}, "stages": stages,
              "max_sustained": best, "peak_throughput": peak}

    lines = ["PRUEBA DE CARGA: rampa por escalones (bucle cerrado, sin pausas entre peticiones)", "=" * 118,
             f"{N_ACCOUNTS} cuentas distintas | mezcla 70% transferencias / 20% saldo / 10% historial | SLO: p95 < {SLO_P95_MS} ms y errores < {SLO_ERROR_RATE:.0%}", "",
             f"{'Usuarios':>8} {'Req/s':>8} {'TPS transf.':>12} {'p50':>7} {'p95':>7} {'p99':>7} {'% error':>8} {'cola pool (máx)':>16} {'pool en uso':>12}  SLO",
             "-" * 118]
    for s in stages:
        ok = s["transfer_p95_ms"] < SLO_P95_MS and s["error_rate"] < SLO_ERROR_RATE
        lines.append(f"{s['users']:>8} {s['requests_per_second']:>8} {s['transfers_per_second']:>12} {s['transfer_p50_ms']:>5}ms {s['transfer_p95_ms']:>5}ms {s['transfer_p99_ms']:>5}ms "
                     f"{s['error_rate']:>8.2%} {str(s['pool_waiting_max']):>16} {str(s['pool_checked_out_avg']):>12}  {'CUMPLE' if ok else 'NO CUMPLE'}")
    lines.append("")
    if best:
        lines += [f"TPS MÁXIMO SOSTENIDO (cumpliendo el SLO): {best['transfers_per_second']} transferencias/s ({best['requests_per_second']} peticiones/s en total) con {best['users']} usuarios simultáneos",
                  f"   latencia de transferencias en ese punto: p50 {best['transfer_p50_ms']} ms | p95 {best['transfer_p95_ms']} ms | p99 {best['transfer_p99_ms']} ms | errores {best['error_rate']:.2%}"]
    else:
        lines.append("Ningún escalón cumplió el SLO.")
    if peak:
        lines.append(f"TPS PICO (aunque ya sin cumplir el SLO): {peak['transfers_per_second']} transferencias/s con {peak['users']} usuarios (p95 {peak['transfer_p95_ms']} ms)")
    text = "\n".join(lines)
    print("\n" + text)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "load_report.txt").write_text(text + "\n", encoding="utf-8")
    (OUT_DIR / "load_report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
