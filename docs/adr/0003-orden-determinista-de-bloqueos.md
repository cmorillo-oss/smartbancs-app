# ADR 0003 — Orden determinista de bloqueos (`account_id` ascendente)

- **Estado:** Aceptado · **Fase:** 3 · **Es la decisión arquitectónica central del proyecto**

## Contexto
Con bloqueo pesimista (ADR 0002), una transferencia bloquea DOS cuentas. Si el orden depende de la
dirección (origen primero, destino después):
- T1: A → B bloquea A y pide B.
- T2: B → A bloquea B y pide A.

Ninguna puede continuar: **deadlock**. PostgreSQL lo detecta tras `deadlock_timeout` y aborta una de
las dos (SQLSTATE `40P01`). En el pico de quincena descrito en el reto (transferencias cruzadas
masivas) esto explica "transferencias que no se completan", latencia alta y conexiones retenidas.

## Decisión
Toda transferencia bloquea sus dos cuentas **en una sola sentencia** con orden explícito:

```sql
SELECT ... FROM accounts WHERE id = ANY(:lock_ids) ORDER BY id FOR UPDATE;
```
y `lock_ids = sorted([source_id, dest_id])`.

## Por qué funciona
Un deadlock exige un **ciclo** de espera. Si todas las transacciones piden los recursos en el mismo
orden global, el grafo de espera no puede tener ciclos: la que llega segunda espera a la primera y
después continúa. Es la técnica clásica de *ordenación de recursos* (filósofos comensales).
En el plan, `LockRows` queda por encima de `Sort`, así que PostgreSQL bloquea en el orden ordenado.

## Evidencia
- 60 transferencias cruzadas A↔B en paralelo: 60 × HTTP 201, `smartbancs_db_deadlocks_total = 0`.
- Con orden invertido a propósito (guion de prueba) PostgreSQL sí devuelve `40P01`; la Fase 7
  reproduce ambos casos lado a lado.

## Red de seguridad
Si aun así ocurre un `40P01` (p. ej. otro código futuro que no respete el orden): se cuenta en
`smartbancs_db_deadlocks_total`, se loguea a ERROR (cuentas, consulta, tiempo de espera, trace_id) y se
reintenta hasta 3 veces con backoff exponencial y *jitter*; después, HTTP 503 `DEADLOCK_RETRY_EXHAUSTED`.

## Consecuencias
- (+) Cero deadlocks por diseño en el flujo de transferencia.
- (−) Es una **convención**: cualquier código nuevo que bloquee varias cuentas debe usar
  `lock_accounts_in_order`. Por eso vive en un único lugar (repositorio) y no se repite.
