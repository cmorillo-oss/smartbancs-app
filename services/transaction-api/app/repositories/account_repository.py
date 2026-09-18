"""Acceso a la tabla accounts."""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LockedAccount


async def resolve_ids(session: AsyncSession, numbers: list[str]) -> dict[str, int]:
    """account_number -> id, SIN bloquear.

    Es seguro leer sin bloqueo porque `id` y `account_number` son inmutables: nunca cambian
    tras crear la cuenta. Lo que sí cambia (saldo, estado) se lee más tarde, ya bloqueado.
    """
    rows = await session.execute(
        text("SELECT id, account_number FROM accounts WHERE account_number = ANY(:nums)"),
        {"nums": numbers},
    )
    return {r.account_number: r.id for r in rows}


async def lock_accounts_in_order(session: AsyncSession, account_ids: list[int]) -> dict[int, LockedAccount]:
    """Bloquea las cuentas con FOR UPDATE en orden ascendente de id.

    ======================================================================================
    ⭐ DECISIÓN CENTRAL DEL PROYECTO: ORDEN DETERMINISTA DE BLOQUEO ⭐
    ======================================================================================
    El problema (deadlock clásico):
        Transferencia T1: A -> B   bloquea A, luego pide B
        Transferencia T2: B -> A   bloquea B, luego pide A
    Si ocurren a la vez: T1 tiene A y espera B; T2 tiene B y espera A. Ninguna puede avanzar
    jamás. PostgreSQL lo detecta tras `deadlock_timeout` (1s por defecto) y mata a una de las
    dos con el error 40P01. En un pico de quincena, con miles de transferencias cruzadas,
    esto se traduce en transferencias fallidas, latencia disparada y conexiones retenidas.

    La solución: un orden global.
        Si TODA transferencia adquiere sus bloqueos en orden ascendente de account_id,
        no puede existir un ciclo de espera. T1 y T2 pedirán primero la cuenta de menor id;
        la que llegue segunda simplemente ESPERA a que la primera termine (milisegundos) y
        luego continúa. Esperar en cola es barato; abortar y reintentar es caro.
        (Es la misma técnica que "ordenar los recursos" para evitar deadlocks en el problema
        de los filósofos comensales.)

    Por qué esta sentencia lo garantiza:
        - Se piden AMBAS filas en UNA sola sentencia con ORDER BY id. En el plan de ejecución
          el nodo LockRows queda POR ENCIMA del Sort, es decir, PostgreSQL bloquea las filas
          en el orden en que salen ordenadas. Sin el ORDER BY, el orden de bloqueo dependería
          del plan (índice, escaneo secuencial...) y sería impredecible.
        - `sorted()` en Python es cinturón y tirantes: hace el orden explícito y visible en el
          código y en los logs, y no depende solo del comportamiento del planificador.

    Por qué NO NOWAIT y sí lock_timeout:
        NOWAIT falla al instante si la fila está ocupada, lo que convertiría la contención
        normal en errores. Preferimos esperar un poco (lock_timeout, configurado por el
        servicio) y solo fallar si la espera es anormal, sin colgar el pool de conexiones.
    ======================================================================================
    """
    lock_ids = sorted(account_ids)
    rows = await session.execute(
        text(
            """
            SELECT id, account_number, customer_id, balance, currency, status
              FROM accounts
             WHERE id = ANY(:lock_ids)
             ORDER BY id            -- CRÍTICO: siempre ascendente (ver docstring)
               FOR UPDATE
            """
        ),
        {"lock_ids": lock_ids},
    )
    return {
        r.id: LockedAccount(r.id, r.account_number, r.customer_id, r.balance, r.currency, r.status)
        for r in rows
    }


async def apply_delta(session: AsyncSession, account_id: int, delta: Decimal) -> Decimal:
    """Suma `delta` (negativo para débito) al saldo y devuelve el saldo resultante.

    POR QUÉ `balance = balance + :delta` en SQL y no "leer, sumar en Python, escribir":
    la operación es atómica en la BD y, además, ya tenemos la fila bloqueada. Con el bloqueo
    da igual, pero si algún día alguien olvida el FOR UPDATE, esta forma no pierde
    actualizaciones (la versión leer-modificar-escribir sí; es lo que muestra el demo de la Fase 4).
    """
    row = await session.execute(
        text(
            """
            UPDATE accounts
               SET balance = balance + :delta,
                   version = version + 1,
                   updated_at = NOW()
             WHERE id = :id
         RETURNING balance
            """
        ),
        {"delta": delta, "id": account_id},
    )
    return row.scalar_one()


async def get_by_number(session: AsyncSession, account_number: str):
    row = await session.execute(
        text(
            """
            SELECT account_number, customer_id, balance, currency, status, version, updated_at
              FROM accounts WHERE account_number = :n
            """
        ),
        {"n": account_number},
    )
    return row.mappings().first()
