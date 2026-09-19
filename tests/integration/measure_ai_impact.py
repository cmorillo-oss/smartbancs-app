"""Mide la latencia de las transferencias con el servicio de IA en distintos estados.

Es el dato que responde "¿qué pasa si la IA se cae?": la latencia de la transferencia debe ser
la misma con la IA encendida, apagada o colgada.

  python integration/measure_ai_impact.py --label ai_on       # mide y guarda evidence/ai-resilience/ai_on.json
  python integration/measure_ai_impact.py --compare           # tabla comparativa de las mediciones guardadas

Lo orquesta scripts/ai_resilience_demo.sh (que es quien enciende/apaga el contenedor de la IA).

Dos perfiles de carga, ambos sobre el MISMO conjunto de operaciones (semilla fija):
  sostenida: 100 transferencias con 10 en vuelo a la vez (mide el tiempo de servicio normal)
  rafaga:    100 transferencias TODAS a la vez (mide comportamiento bajo cola/contención)
Cada perfil se repite ROUNDS veces: con una sola muestra el ruido podría confundirse con un efecto.
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg  # noqa: E402

from common import DATABASE_DSN, api_transfer, create_accounts, new_client  # noqa: E402

OUT_DIR = Path(os.environ.get("EVIDENCE_AI_DIR", "/evidence/ai-resilience"))
ROUNDS = 3
N = 100
PROFILES = {"sostenida": 10, "rafaga": N}
SLO_MS = 2000  # requisito del reto: transferencias en < 2 segundos


def percentile(sorted_values: list[float], p: float) -> float:
    """Percentil con interpolación lineal (mismo método que numpy por defecto)."""
    k = (len(sorted_values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def stats(latencies_ms: list[float]) -> dict:
    s = sorted(latencies_ms)
    return {
        "count": len(s), "p50_ms": round(percentile(s, 50), 1), "p95_ms": round(percentile(s, 95), 1),
        "p99_ms": round(percentile(s, 99), 1), "max_ms": round(s[-1], 1), "mean_ms": round(sum(s) / len(s), 1),
    }


async def read_metrics(client) -> dict:
    text = (await client.get("/metrics")).text
    calls = {m.group(1): float(m.group(2)) for m in re.finditer(r'^smartbancs_ai_calls_total\{status="(\w+)"\} (\S+)', text, re.M)}
    breaker = re.search(r"^smartbancs_ai_circuit_breaker_state (\S+)", text, re.M)
    dsum = re.search(r"^smartbancs_ai_call_duration_seconds_sum (\S+)", text, re.M)
    dcount = re.search(r"^smartbancs_ai_call_duration_seconds_count (\S+)", text, re.M)
    return {"ai_calls": calls, "breaker_state": float(breaker.group(1)) if breaker else None,
            "ai_call_seconds_sum": float(dsum.group(1)) if dsum else 0.0,
            "ai_call_seconds_count": float(dcount.group(1)) if dcount else 0.0}


async def run_profile(client, accounts, concurrency: int, seed: int) -> tuple[list[float], dict]:
    rng = random.Random(seed)
    plan = []
    for _ in range(N):
        a, b = rng.sample(accounts, 2)
        plan.append((a["number"], b["number"], Decimal(rng.randint(1, 50))))
    sem = asyncio.Semaphore(concurrency)
    latencies, codes = [], {}

    async def one(src, dst, amount):
        async with sem:
            t = time.perf_counter()
            r = await api_transfer(client, src, dst, amount)
            latencies.append((time.perf_counter() - t) * 1000)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1

    started = time.perf_counter()
    await asyncio.gather(*(one(*p) for p in plan))
    return latencies, {"http_status_counts": codes, "wall_seconds": round(time.perf_counter() - started, 2)}


async def measure(label: str) -> None:
    conn = await asyncpg.connect(DATABASE_DSN)
    async with new_client() as client:
        # Cuentas nuevas con saldo de sobra: ninguna transferencia falla por fondos, así que las
        # 100 deben ser 201 y la comparación es limpia.
        accounts = await create_accounts(conn, 20, Decimal("1000000.00"), prefix="AI")
        # Calentamiento (no se mide): conexiones del pool, caché de planes de Postgres y, con la IA
        # apagada, la apertura del circuit breaker tras sus primeros fallos.
        for i in range(10):
            await api_transfer(client, accounts[i]["number"], accounts[i + 1]["number"], Decimal("1"))
        await asyncio.sleep(3)

        before = await read_metrics(client)
        result = {"label": label, "measured_at": datetime.now(timezone.utc).isoformat(), "profiles": {}}
        for name, conc in PROFILES.items():
            rounds, pooled, all_codes = [], [], {}
            for rnd in range(ROUNDS):
                lat, info = await run_profile(client, accounts, conc, seed=1000 + rnd)
                rounds.append({**stats(lat), **info})
                pooled += lat
                for k, v in info["http_status_counts"].items():
                    all_codes[k] = all_codes.get(k, 0) + v
                await asyncio.sleep(1)
            result["profiles"][name] = {
                "concurrency": conc, "transfers_per_round": N, "rounds": rounds,
                "pooled": stats(pooled), "http_status_counts": all_codes,
            }
        after = await read_metrics(client)

    d_calls = {k: after["ai_calls"].get(k, 0) - before["ai_calls"].get(k, 0) for k in after["ai_calls"]}
    d_sum = after["ai_call_seconds_sum"] - before["ai_call_seconds_sum"]
    d_cnt = after["ai_call_seconds_count"] - before["ai_call_seconds_count"]
    result["ai_activity_during_measurement"] = {
        "ai_calls_delta_by_status": d_calls,
        "avg_ai_call_ms": round(d_sum / d_cnt * 1000, 1) if d_cnt else None,
        "circuit_breaker_state_after": {0: "CLOSED", 1: "OPEN", 2: "HALF_OPEN"}.get(int(after["breaker_state"] or 0)),
    }
    await conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{label}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{label}] guardado en {OUT_DIR / (label + '.json')}")
    for name, prof in result["profiles"].items():
        p = prof["pooled"]
        print(f"  {name:<10} p50={p['p50_ms']:>7}ms  p95={p['p95_ms']:>7}ms  p99={p['p99_ms']:>7}ms  http={prof['http_status_counts']}")
    print(f"  actividad de IA durante la medición: {result['ai_activity_during_measurement']}")


def compare() -> int:
    from statistics import median

    def load(prefix: str) -> list[tuple[str, dict]]:
        return [(f.stem, json.loads(f.read_text(encoding="utf-8"))) for f in sorted(OUT_DIR.glob(f"{prefix}*.json")) if f.stem != "comparison"]

    ons, offs, hungs = load("ai_on_"), load("ai_off_"), load("ai_hung")
    inproc = load("ai_inproc_")
    if not ons or not offs:
        print("Faltan mediciones ai_on_N y/o ai_off_N")
        return 1

    lines = ["COMPARATIVO DE LATENCIA DE TRANSFERENCIAS SEGÚN EL ESTADO DE LA IA", "=" * 100,
             f"Cada fila = 1 medición de {ROUNDS} rondas x {N} transferencias. Condiciones ALTERNADAS (on, off, on, off...) para repartir el ruido.",
             "La cifra que se compara es la MEDIANA de los p95 de las repeticiones (robusta ante una repetición atípica)."]
    header = f"{'Medición':<16}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>10}   p95 por ronda          HTTP"
    summary, verdicts = {}, []

    def emit(prof: str, group: list) -> list[float]:
        p95s = []
        for name, d in group:
            pr = d["profiles"][prof]
            p = pr["pooled"]
            worst = max(r["max_ms"] for r in pr["rounds"])
            rp95 = " / ".join(f"{r['p95_ms']:.0f}" for r in pr["rounds"])
            lines.append(f"{name:<16}{p['p50_ms']:>7.0f}ms{p['p95_ms']:>7.0f}ms{p['p99_ms']:>7.0f}ms{worst:>8.0f}ms   {rp95:<22} {pr['http_status_counts']}")
            p95s.append(p["p95_ms"])
        return p95s

    for prof in PROFILES:
        conc = ons[0][1]["profiles"][prof]["concurrency"]
        lines += ["", f"PERFIL {prof.upper()}: {N} transferencias, {conc} simultáneas", header, "-" * 100]
        on_p95 = emit(prof, ons)
        off_p95 = emit(prof, offs)
        hung_p95 = emit(prof, hungs) if hungs else []
        m_on, m_off = median(on_p95), median(off_p95)
        m_hung = median(hung_p95) if hung_p95 else None
        ok = m_off <= m_on * 1.15 and (m_hung is None or m_hung <= m_on * 1.15)
        verdicts.append(ok)
        lines += [f"  Mediana de p95:  encendida {m_on:.0f}ms   apagada {m_off:.0f}ms ({(m_off - m_on) / m_on * 100:+.1f}%)"
                  + (f"   colgada {m_hung:.0f}ms ({(m_hung - m_on) / m_on * 100:+.1f}%)" if m_hung else ""),
                  f"  Criterio A (apagada/colgada <= 1.15 x encendida): {'CUMPLE' if ok else 'NO CUMPLE'}",
                  f"  Criterio B (SLO p95 < {SLO_MS}ms, IA apagada): {'CUMPLE' if m_off < SLO_MS else 'NO CUMPLE (capacidad de una instancia, no de la IA)'}"]
        summary[prof] = {"p95_encendida_por_repeticion": on_p95, "p95_apagada_por_repeticion": off_p95,
                         "p95_colgada": hung_p95, "mediana_encendida": m_on, "mediana_apagada": m_off,
                         "mediana_colgada": m_hung, "criterio_A": ok}
        if inproc:
            lines += ["", f"  Modo BackgroundTasks (AI_NOTIFY_IN_PROCESS=true, paso 10 literal) - perfil {prof}:", "  " + header]
            emit(prof, inproc)

    lines += ["", "Coste de una IA SÍNCRONA: cada llamada tarda entre 300 y 800 ms (mock; media medida ~549 ms), que se sumarían a CADA transferencia.",
              "Criterio A = la caída/cuelgue de la IA no empeora la latencia (tolerancia 15% para el ruido de medición).",
              "Criterio B = requisito del reto (< 2 s); en ráfaga de 100 simultáneas lo limita la capacidad de UNA instancia, no la IA."]
    text = "\n".join(lines)
    print(text)
    (OUT_DIR / "comparison.txt").write_text(text + "\n", encoding="utf-8")
    (OUT_DIR / "comparison.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if all(verdicts) else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()
    if args.compare:
        sys.exit(compare())
    if not args.label:
        ap.error("indique --label o --compare")
    asyncio.run(measure(args.label))
