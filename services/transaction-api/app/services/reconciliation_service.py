"""Conciliación: compara los saldos locales con los de Bancs e informa de discrepancias.

Es la red de seguridad de la CONSISTENCIA EVENTUAL: como el saldo local se adelanta a Bancs (ADR 0004 y
0008), hace falta poder comprobar que ambos convergen. Es una operación ADMINISTRATIVA:
  * consulta a Bancs cuenta por cuenta, así que se limita en cantidad (máx. 50) y en concurrencia (2);
    nunca se usa en el camino de las transferencias (restricción 2 del reto);
  * distingue una diferencia NORMAL (hay cambios pendientes de sincronizar) de una discrepancia REAL.
"""
import asyncio
from decimal import Decimal

from app.config import settings
from app.errors import DomainError
from app.repositories import outbox_repository as outbox
from app.services import bancs_client


class BancsUnavailable(DomainError):
    status_code = 503
    error_code = "BANCS_UNAVAILABLE"


async def reconcile(session, limit: int, account_numbers: list[str] | None) -> dict:
    limit = max(1, min(limit, settings.reconciliation_max_accounts))
    if not bancs_client.breaker.would_allow():
        raise BancsUnavailable("el circuito hacia Bancs está abierto; la conciliación se reintentará más tarde")

    accounts = await outbox.accounts_for_reconciliation(session, limit, account_numbers)
    pending = await outbox.pending_bancs_by_account(session)
    sem = asyncio.Semaphore(settings.reconciliation_concurrency)

    async def check(row) -> dict:
        number, local = row["account_number"], row["balance"]
        async with sem:
            status, bancs_balance = await bancs_client.get_balance(number)
        n_pending = pending.get(number, 0)
        # Los montos salen como TEXTO ("4880.00"): en JSON un número es un float y el dinero nunca lo es.
        item = {"account_number": number, "local_balance": str(local),
                "bancs_balance": None if bancs_balance is None else str(bancs_balance.quantize(Decimal("0.01"))),
                "pending_events": n_pending}
        if status in ("failed", "circuit_open"):
            item["result"] = "BANCS_ERROR"
        elif status == "not_found":
            # Bancs no conoce la cuenta: normal si sus cambios aún no se han sincronizado.
            item["result"] = "PENDING_SYNC" if n_pending else "NOT_IN_BANCS"
        elif bancs_balance == local:
            item["result"] = "MATCH"
        else:
            # Diferencia con cambios pendientes = consistencia eventual (esperada, se cerrará sola).
            # Diferencia SIN nada pendiente = discrepancia real: alguien debe investigarla.
            item["result"] = "PENDING_SYNC" if n_pending else "DISCREPANCY"
            item["difference"] = str(local - (bancs_balance or Decimal(0)))
        return item

    items = await asyncio.gather(*(check(r) for r in accounts))
    summary: dict[str, int] = {}
    for it in items:
        summary[it["result"]] = summary.get(it["result"], 0) + 1
    return {
        "checked": len(items), "summary": summary,
        "discrepancies": [i for i in items if i["result"] == "DISCREPANCY"],
        "items": items,
    }
