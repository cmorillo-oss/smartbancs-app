"""Fase 4: prueba empírica de que NO hay race conditions.

Cada test ataca la API real (HTTP) con carga concurrente y luego verifica el estado
REAL de la base de datos. Las salidas se guardan en evidence/test-data/*.json.
"""
import random
import time
import uuid
from collections import Counter
from decimal import Decimal

from common import (
    api_transfer,
    create_accounts,
    deadlocks_counter,
    run_concurrently,
    save_evidence,
    total_balance,
)


async def test_sobregiro_concurrente(client, db):
    """100 transferencias simultáneas de 50.00 sobre una cuenta con 1000.00.

    Solo 20 pueden triunfar (20 x 50 = 1000). Si hubiera una race condition, varias
    transferencias leerían el mismo saldo y triunfarían de más (sobregiro / dinero fantasma).
    """
    # Cuentas fijadas por el brief (seed). Se restablecen para que la prueba sea repetible.
    await db.execute("UPDATE accounts SET balance = 1000.00 WHERE account_number = 'ACC-TEST-CONCURRENCY'")
    await db.execute("UPDATE accounts SET balance = 0.00 WHERE account_number = 'ACC-TEST-SINK'")
    src, dst = "ACC-TEST-CONCURRENCY", "ACC-TEST-SINK"
    t0 = await db.fetchval("SELECT now()")

    started = time.perf_counter()
    responses = await run_concurrently(
        [api_transfer(client, src, dst, Decimal("50.00")) for _ in range(100)]
    )
    elapsed = time.perf_counter() - started

    codes = Counter(r.status_code for r in responses)
    error_codes = Counter(r.json().get("error_code") for r in responses if r.status_code != 201)
    src_balance = await db.fetchval("SELECT balance FROM accounts WHERE account_number = $1", src)
    dst_balance = await db.fetchval("SELECT balance FROM accounts WHERE account_number = $1", dst)
    completed = await db.fetchval(
        "SELECT count(*) FROM transactions t JOIN accounts a ON a.id = t.source_account_id "
        "WHERE a.account_number = $1 AND t.created_at >= $2 AND t.status = 'COMPLETED'", src, t0)

    save_evidence("01_sobregiro_concurrente", {
        "requests": 100, "amount_each": "50.00", "elapsed_seconds": round(elapsed, 3),
        "http_status_counts": dict(codes), "error_code_counts": dict(error_codes),
        "final_source_balance": src_balance, "final_sink_balance": dst_balance,
        "completed_transactions_in_db": completed,
    })

    assert codes[201] == 20, f"deben triunfar exactamente 20, triunfaron {codes[201]}"
    assert codes[422] == 80 and error_codes == {"INSUFFICIENT_FUNDS": 80}
    assert set(codes) == {201, 422}, f"no debe haber errores 5xx: {codes}"
    assert src_balance == Decimal("0.00"), "ni un centavo debe quedar en el origen"
    assert dst_balance == Decimal("1000.00"), "ni un centavo perdido ni creado en el destino"
    assert completed == 20


async def test_suma_cero(client, db):
    """200 transferencias aleatorias entre 20 cuentas en paralelo: el dinero total no cambia."""
    accounts = await create_accounts(db, 20, Decimal("10000.00"), prefix="T2")
    ids = [a["id"] for a in accounts]
    total_before = await total_balance(db, ids)
    deadlocks_before = await deadlocks_counter(client)

    rng = random.Random(42)  # semilla fija: si falla, el mismo escenario se puede reproducir
    jobs = []
    for _ in range(200):
        a, b = rng.sample(accounts, 2)  # origen y destino aleatorios y distintos => tráfico cruzado
        jobs.append(api_transfer(client, a["number"], b["number"], Decimal(rng.randint(1, 500))))

    started = time.perf_counter()
    responses = await run_concurrently(jobs)
    elapsed = time.perf_counter() - started

    codes = Counter(r.status_code for r in responses)
    total_after = await total_balance(db, ids)
    deadlocks = await deadlocks_counter(client) - deadlocks_before
    negatives = await db.fetchval("SELECT count(*) FROM accounts WHERE id = ANY($1) AND balance < 0", ids)

    save_evidence("02_suma_cero", {
        "transfers": 200, "accounts": 20, "elapsed_seconds": round(elapsed, 3),
        "http_status_counts": dict(codes), "total_before": total_before,
        "total_after": total_after, "deadlocks_detected": deadlocks,
    })

    assert set(codes) <= {201, 422}, f"errores inesperados: {codes}"
    assert total_after == total_before, f"el dinero cambió: {total_before} -> {total_after}"
    assert negatives == 0
    assert deadlocks == 0, "con orden determinista de bloqueo no debe haber ningún deadlock"


async def test_idempotencia_bajo_concurrencia(client, db):
    """50 peticiones simultáneas con la MISMA Idempotency-Key: se crea exactamente 1 transacción."""
    accounts = await create_accounts(db, 2, Decimal("1000.00"), prefix="T3")
    src, dst = accounts
    key = f"idem-{uuid.uuid4()}"

    responses = await run_concurrently(
        [api_transfer(client, src["number"], dst["number"], Decimal("100.00"), key=key) for _ in range(50)]
    )

    codes = Counter(r.status_code for r in responses)
    tx_ids = {r.json()["transaction_id"] for r in responses if r.status_code in (200, 201)}
    tx_count = await db.fetchval("SELECT count(*) FROM transactions WHERE idempotency_key = $1", key)
    ledger_count = await db.fetchval(
        "SELECT count(*) FROM ledger_entries l JOIN transactions t ON t.id = l.transaction_id "
        "WHERE t.idempotency_key = $1", key)
    src_balance = await db.fetchval("SELECT balance FROM accounts WHERE id = $1", src["id"])
    replays = sum(1 for r in responses if r.headers.get("Idempotent-Replay") == "true")

    save_evidence("03_idempotencia_concurrente", {
        "requests": 50, "http_status_counts": dict(codes), "replay_headers": replays,
        "distinct_transaction_ids_returned": len(tx_ids), "transactions_in_db": tx_count,
        "ledger_entries_in_db": ledger_count, "final_source_balance": src_balance,
    })

    assert tx_count == 1, f"debe existir exactamente 1 transacción, hay {tx_count}"
    assert codes[201] == 1 and codes[200] == 49, f"1 x 201 y 49 x 200 esperados: {codes}"
    assert replays == 49
    assert len(tx_ids) == 1, "todas las respuestas deben apuntar a la misma transacción"
    assert ledger_count == 2
    assert src_balance == Decimal("900.00"), "el dinero solo se movió UNA vez"


async def test_integridad_del_ledger(client, db):
    """Partida doble: SUM(DEBIT) == SUM(CREDIT), global y por transacción; y cada saldo == último balance_after."""
    accounts = await create_accounts(db, 10, Decimal("5000.00"), prefix="T4")
    ids = [a["id"] for a in accounts]
    rng = random.Random(7)
    jobs = []
    for _ in range(100):
        a, b = rng.sample(accounts, 2)
        jobs.append(api_transfer(client, a["number"], b["number"], Decimal(rng.randint(1, 300))))
    responses = await run_concurrently(jobs)
    assert all(r.status_code in (201, 422) for r in responses)

    # 1) Global: en TODO el libro, lo debitado es exactamente lo acreditado.
    totals = {r["entry_type"]: r["s"] for r in await db.fetch(
        "SELECT entry_type, SUM(amount) s FROM ledger_entries GROUP BY entry_type")}

    # 2) Por transacción: exactamente 1 DEBIT y 1 CREDIT del mismo importe.
    bad_pairs = await db.fetchval(
        """
        SELECT count(*) FROM (
            SELECT transaction_id FROM ledger_entries
             WHERE account_id = ANY($1)
             GROUP BY transaction_id
            HAVING count(*) <> 2
                OR count(*) FILTER (WHERE entry_type='DEBIT') <> 1
                OR count(*) FILTER (WHERE entry_type='CREDIT') <> 1
                OR min(amount) <> max(amount)
        ) x
        """, ids)

    # 3) Por cuenta: el saldo actual debe ser el saldo inicial + créditos - débitos, y coincidir
    #    con el balance_after del último asiento (el libro reconstruye el saldo).
    mismatches = await db.fetchval(
        """
        SELECT count(*) FROM accounts a
         WHERE a.id = ANY($1)
           AND a.balance <> 5000.00
                          + COALESCE((SELECT SUM(amount) FROM ledger_entries WHERE account_id=a.id AND entry_type='CREDIT'),0)
                          - COALESCE((SELECT SUM(amount) FROM ledger_entries WHERE account_id=a.id AND entry_type='DEBIT'),0)
        """, ids)
    stale_snapshots = await db.fetchval(
        """
        SELECT count(*) FROM accounts a
         WHERE a.id = ANY($1)
           AND EXISTS (SELECT 1 FROM ledger_entries WHERE account_id = a.id)
           AND a.balance <> (SELECT balance_after FROM ledger_entries WHERE account_id=a.id ORDER BY id DESC LIMIT 1)
        """, ids)
    # 4) Cada transacción COMPLETED tiene su par de asientos.
    orphans = await db.fetchval(
        """
        SELECT count(*) FROM transactions t
         WHERE (t.source_account_id = ANY($1) OR t.dest_account_id = ANY($1))
           AND t.status = 'COMPLETED'
           AND (SELECT count(*) FROM ledger_entries WHERE transaction_id = t.id) <> 2
        """, ids)

    save_evidence("04_integridad_ledger", {
        "global_sum_debit": totals.get("DEBIT"), "global_sum_credit": totals.get("CREDIT"),
        "transactions_with_bad_entry_pairs": bad_pairs,
        "accounts_whose_balance_differs_from_ledger": mismatches,
        "accounts_whose_last_balance_after_differs": stale_snapshots,
        "completed_transactions_without_two_entries": orphans,
    })

    assert totals["DEBIT"] == totals["CREDIT"], f"libro descuadrado: {totals}"
    assert bad_pairs == 0
    assert mismatches == 0
    assert stale_snapshots == 0
    assert orphans == 0
