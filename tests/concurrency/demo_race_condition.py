"""Demo para el video: la MISMA carga contra una implementación INSEGURA y contra la SEGURA.

  Escenario A (sobregiro): 100 transferencias simultáneas de 50.00 desde una cuenta con 1000.00.
                            Correcto: 20 éxitos, origen en 0.00, destino en 1000.00.
  Escenario B (suma cero):  200 transferencias aleatorias entre 20 cuentas con 10000.00 c/u.
                            Correcto: el dinero total no cambia (200000.00).

- INSEGURA: se ejecuta directo contra la BD con el patrón clásico "leer-modificar-escribir":
    SELECT saldo  ->  validar en Python  ->  UPDATE saldo = valor_calculado
  (sin FOR UPDATE, y ni siquiera meterlo en una transacción lo salva: en READ COMMITTED dos
  transacciones leen el mismo saldo y la última en escribir pisa a la otra).
- SEGURA: la API real (POST /api/v1/transactions) con bloqueo ordenado FOR UPDATE.

Uso:  make demo-race        (o: python tests/concurrency/demo_race_condition.py)
"""
import asyncio
import os
import random
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg  # noqa: E402

from common import (  # noqa: E402
    DATABASE_DSN,
    api_transfer,
    create_accounts,
    new_client,
    run_concurrently,
    save_evidence,
    total_balance,
)

# Simula el tiempo que tarda una aplicación real entre "leí el saldo" y "escribo el nuevo saldo"
# (reglas de negocio, antifraude, otra consulta...). Con 0 la carrera existe igual, pero se
# manifiesta menos; 5 ms es un valor modesto y realista para cualquier lógica de negocio.
THINK_TIME_S = 0.005

# Deadlocks reales que PostgreSQL detecta en la versión insegura (se reinicia por escenario).
UNSAFE_DEADLOCKS = {"count": 0}


async def unsafe_transfer(pool, src_id: int, dst_id: int, amount: Decimal) -> bool:
    """LA VERSIÓN CON BUG. No la copies: existe para mostrar qué NO hacer."""
    try:
        return await _unsafe_transfer(pool, src_id, dst_id, amount)
    except asyncpg.exceptions.DeadlockDetectedError:
        # BONUS de la versión insegura: sus UPDATE toman los bloqueos en el orden origen->destino,
        # sin orden global. En transferencias cruzadas (A->B y B->A) PostgreSQL detecta un deadlock
        # REAL (40P01) y aborta una. Esa transferencia se pierde (rollback): no mueve dinero.
        UNSAFE_DEADLOCKS["count"] += 1
        return False


async def _unsafe_transfer(pool, src_id: int, dst_id: int, amount: Decimal) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():  # incluso dentro de una transacción falla (READ COMMITTED)
            src_balance = await conn.fetchval("SELECT balance FROM accounts WHERE id=$1", src_id)  # sin FOR UPDATE
            if src_balance < amount:
                return False
            dst_balance = await conn.fetchval("SELECT balance FROM accounts WHERE id=$1", dst_id)
            await asyncio.sleep(THINK_TIME_S)  # <- ventana de la carrera: otros leen el mismo saldo viejo
            # Escribe un valor CALCULADO con datos posiblemente obsoletos: pisa lo que otros escribieron.
            await conn.execute("UPDATE accounts SET balance=$1 WHERE id=$2", src_balance - amount, src_id)
            await conn.execute("UPDATE accounts SET balance=$1 WHERE id=$2", dst_balance + amount, dst_id)
            return True


async def _accounts(pool, n: int, balance: Decimal, prefix: str):
    async with pool.acquire() as conn:
        return await create_accounts(conn, n, balance, prefix)


async def run_overdraft(mode: str, pool, client) -> dict:
    (src,) = await _accounts(pool, 1, Decimal("1000.00"), f"D{mode[0]}")
    (dst,) = await _accounts(pool, 1, Decimal("0.00"), f"D{mode[0]}")
    started = time.perf_counter()
    if mode == "inseguro":
        results = await run_concurrently([unsafe_transfer(pool, src["id"], dst["id"], Decimal("50")) for _ in range(100)])
        ok = sum(results)
    else:
        results = await run_concurrently(
            [api_transfer(client, src["number"], dst["number"], Decimal("50")) for _ in range(100)])
        ok = sum(1 for r in results if r.status_code == 201)
    elapsed = time.perf_counter() - started
    async with pool.acquire() as conn:
        src_bal = await conn.fetchval("SELECT balance FROM accounts WHERE id=$1", src["id"])
        dst_bal = await conn.fetchval("SELECT balance FROM accounts WHERE id=$1", dst["id"])
    deadlocks = UNSAFE_DEADLOCKS["count"]; UNSAFE_DEADLOCKS["count"] = 0
    return {
        "deadlocks": deadlocks, "exitos": ok, "saldo_origen": src_bal, "saldo_destino": dst_bal,
        "dinero_total": src_bal + dst_bal, "dinero_fantasma": src_bal + dst_bal - Decimal("1000.00"),
        "segundos": round(elapsed, 2),
    }


async def run_zero_sum(mode: str, pool, client) -> dict:
    accounts = await _accounts(pool, 20, Decimal("10000.00"), f"Z{mode[0]}")
    ids = [a["id"] for a in accounts]
    rng = random.Random(42)  # misma semilla => EXACTAMENTE las mismas 200 transferencias en ambos modos
    plan = [(*rng.sample(accounts, 2), Decimal(rng.randint(1, 500))) for _ in range(200)]
    async with pool.acquire() as conn:
        before = await total_balance(conn, ids)
    started = time.perf_counter()
    if mode == "inseguro":
        results = await run_concurrently([unsafe_transfer(pool, a["id"], b["id"], amt) for a, b, amt in plan])
        ok = sum(results)
    else:
        results = await run_concurrently([api_transfer(client, a["number"], b["number"], amt) for a, b, amt in plan])
        ok = sum(1 for r in results if r.status_code == 201)
    elapsed = time.perf_counter() - started
    async with pool.acquire() as conn:
        after = await total_balance(conn, ids)
    deadlocks = UNSAFE_DEADLOCKS["count"]; UNSAFE_DEADLOCKS["count"] = 0
    return {"deadlocks": deadlocks, "exitos": ok, "dinero_antes": before, "dinero_despues": after,
            "dinero_fantasma": after - before, "segundos": round(elapsed, 2)}


def print_side_by_side(title: str, rows: list[tuple[str, object, object]], verdict_unsafe: str, verdict_safe: str):
    print(f"\n{title}")
    print("=" * 78)
    print(f"{'':34}{'INSEGURO (sin FOR UPDATE)':<27}{'SEGURO (API real)':<17}")
    print("-" * 78)
    for label, a, b in rows:
        print(f"{label:<34}{str(a):<27}{str(b):<17}")
    print("-" * 78)
    print(f"{'Veredicto':<34}{verdict_unsafe:<27}{verdict_safe:<17}")


async def main() -> int:
    pool = await asyncpg.create_pool(DATABASE_DSN, min_size=10, max_size=60)
    async with new_client() as client:
        a_unsafe = await run_overdraft("inseguro", pool, client)
        a_safe = await run_overdraft("seguro", pool, client)
        b_unsafe = await run_zero_sum("inseguro", pool, client)
        b_safe = await run_zero_sum("seguro", pool, client)
    await pool.close()

    print("\nDEMO: CONDICIÓN DE CARRERA (race condition) EN TRANSFERENCIAS")
    print_side_by_side(
        "ESCENARIO A: 100 transferencias simultáneas de 50.00 sobre una cuenta con 1000.00 (deben triunfar 20)",
        [
            ("Transferencias exitosas", a_unsafe["exitos"], a_safe["exitos"]),
            ("  (correcto: 20)", "", ""),
            ("Saldo final origen", a_unsafe["saldo_origen"], a_safe["saldo_origen"]),
            ("  (correcto: 0.00)", "", ""),
            ("Saldo final destino", a_unsafe["saldo_destino"], a_safe["saldo_destino"]),
            ("  (correcto: 1000.00)", "", ""),
            ("DINERO FANTASMA (total-1000)", a_unsafe["dinero_fantasma"], a_safe["dinero_fantasma"]),
        ],
        "CORRUPTO" if a_unsafe["exitos"] != 20 or a_unsafe["dinero_fantasma"] != 0 else "sin fallo visible",
        "CORRECTO" if a_safe["exitos"] == 20 and a_safe["dinero_fantasma"] == 0 else "FALLO",
    )
    print(f"\n  El sistema INSEGURO confirmó {a_unsafe['exitos']} transferencias de 50.00 "
          f"= {a_unsafe['exitos'] * 50} prometidos a clientes, con solo 1000.00 disponibles.")

    print_side_by_side(
        "ESCENARIO B: 200 transferencias aleatorias entre 20 cuentas (el dinero total debe conservarse)",
        [
            ("Dinero total antes", b_unsafe["dinero_antes"], b_safe["dinero_antes"]),
            ("Dinero total después", b_unsafe["dinero_despues"], b_safe["dinero_despues"]),
            ("DINERO FANTASMA (después-antes)", b_unsafe["dinero_fantasma"], b_safe["dinero_fantasma"]),
            ("Transferencias exitosas", b_unsafe["exitos"], b_safe["exitos"]),
            ("Deadlocks de PostgreSQL", b_unsafe["deadlocks"], "0 (orden fijo)"),
        ],
        "CORRUPTO" if b_unsafe["dinero_fantasma"] != 0 else "sin fallo visible",
        "CORRECTO" if b_safe["dinero_fantasma"] == 0 else "FALLO",
    )
    print()

    save_evidence("05_demo_race_condition", {
        "escenario_A_sobregiro": {"inseguro": a_unsafe, "seguro": a_safe},
        "escenario_B_suma_cero": {"inseguro": b_unsafe, "seguro": b_safe},
    })
    # El demo "pasa" si el seguro es correcto; que el inseguro falle es justo lo que se quiere mostrar.
    safe_ok = a_safe["exitos"] == 20 and a_safe["dinero_fantasma"] == 0 and b_safe["dinero_fantasma"] == 0
    return 0 if safe_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
