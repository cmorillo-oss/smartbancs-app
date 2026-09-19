"""Integración: una transferencia acaba reflejada en Bancs (consistencia eventual comprobada de extremo a extremo).

Requiere el stack COMPLETO levantado (API, worker, bancs-mock): `make up`.
"""
import asyncio
import time
from decimal import Decimal

from common import api_transfer, create_accounts, save_evidence


async def _reconcile(client, numbers: list[str]) -> dict:
    r = await client.get("/api/v1/admin/reconciliation", params={"accounts": ",".join(numbers)})
    assert r.status_code == 200, r.text
    return r.json()


async def test_transferencias_se_sincronizan_con_bancs(client, db):
    a, b = await create_accounts(db, 2, Decimal("1000.00"), prefix="BS")
    numbers = [a["number"], b["number"]]

    # 5 transferencias: la API responde sin esperar a Bancs (regla de oro: nunca en el camino crítico).
    for _ in range(5):
        r = await api_transfer(client, a["number"], b["number"], Decimal("10.00"))
        assert r.status_code == 201

    # Consistencia EVENTUAL: al principio Bancs puede no coincidir todavía; debe converger sola.
    deadline, first, report = time.monotonic() + 45, None, None
    while time.monotonic() < deadline:
        report = await _reconcile(client, numbers)
        first = first or report["summary"]
        if report["summary"] == {"MATCH": 2}:
            break
        await asyncio.sleep(2)

    pending = await db.fetchval(
        "SELECT count(*) FROM outbox_events WHERE event_type='bancs.balance_updated' AND status <> 'SENT' "
        "AND (payload->>'source_account' = ANY($1) OR payload->>'dest_account' = ANY($1))", numbers)
    batches = await db.fetchval("SELECT count(*) FROM bancs_sync_log WHERE status = 'SUCCESS'")

    save_evidence("06_sincronizacion_bancs", {
        "resumen_conciliacion_inicial": first, "resumen_conciliacion_final": report["summary"],
        "saldos_finales": [{"cuenta": i["account_number"], "local": i["local_balance"], "bancs": i["bancs_balance"]} for i in report["items"]],
        "eventos_sin_enviar": pending, "lotes_exitosos_en_bancs_sync_log": batches,
    })

    assert report["summary"] == {"MATCH": 2}, f"no convergió: {report}"
    assert report["discrepancies"] == []
    assert pending == 0
    assert batches >= 1
    assert {i["local_balance"] for i in report["items"]} == {"950.00", "1050.00"}


async def test_conciliacion_detecta_discrepancia_real(client, db):
    """Si el saldo local cambia SIN evento pendiente (p. ej. un UPDATE manual erróneo), es una discrepancia REAL."""
    a, b = await create_accounts(db, 2, Decimal("500.00"), prefix="BD")
    assert (await api_transfer(client, a["number"], b["number"], Decimal("50.00"))).status_code == 201
    for _ in range(30):  # esperar a que se sincronice
        if (await _reconcile(client, [a["number"], b["number"]]))["summary"] == {"MATCH": 2}:
            break
        await asyncio.sleep(2)

    await db.execute("UPDATE accounts SET balance = balance + 7.77 WHERE id = $1", a["id"])  # corrupción simulada
    report = await _reconcile(client, [a["number"]])
    assert report["summary"] == {"DISCREPANCY": 1}
    assert report["discrepancies"][0]["difference"] == "7.77"
