# ADR 0008 — Sincronización con Bancs sin saturar al legado

- **Estado:** Aceptado · **Fase:** 6

## Contexto
Bancs (core legado) se degrada si recibe muchas consultas directas. SmartBancs puede generar hasta 10.000
transferencias por segundo, cada una con un cambio de saldo que Bancs debe conocer. Medido con el mock
(`evidence/bancs/degradation.txt`):

| Peticiones simultáneas a Bancs | p95 | Errores 503 | Peticiones OK por segundo |
|---|---|---|---|
| 1 – 10 | ~0.5 s | 0 % | hasta 24.5 |
| 20 | 1.5 s | 78 % | 3.2 |
| 80 | 6.9 s | 68 % | 3.4 |

Pasado su límite, Bancs no solo se vuelve lento: **rinde menos** (colapso). Consultarlo por transferencia es inviable.

## Decisión
1. **El camino crítico nunca habla con Bancs.** Las transferencias operan sobre el saldo local
   (`accounts.balance`, "saldo disponible") y dejan un evento `bancs.balance_updated` en el outbox (ADR 0004).
2. **El worker sincroniza en lotes de hasta 100 eventos** por petición, cada 2 s o de inmediato si el lote sale lleno
   (tope de 10 lotes por ciclo para no acaparar). Una petición cuesta lo mismo (~200-500 ms) lleve 1 o 100 eventos:
   1000 cambios tardaron **4.1 s en 10 lotes frente a 37.7 s en 1000 peticiones**, y a Bancs le llegan 100 veces
   menos peticiones. Con 10.000 TPS, Bancs vería como máximo ~100 peticiones/s de lotes, en vez de 10.000.
3. **Lo que se envía es idempotente y tolerante al desorden.** Cada evento lleva el saldo *absoluto* resultante y su
   `sequence` (id del outbox); Bancs solo lo aplica si es mayor que el último aplicado a esa cuenta. Un reenvío
   (at-least-once) o un evento fuera de orden no corrompe el saldo.
4. **Circuit breaker propio de Bancs + backoff.** Con el circuito abierto el worker no reclama eventos (no consumen
   reintentos ni llegan a `DEAD`); al fallar un lote, cada evento sube su contador con backoff exponencial y jitter.
   Concurrencia máxima del cliente hacia Bancs: 10 conexiones, justo el umbral que el legado tolera.
5. **Cada lote se registra en `bancs_sync_log`** (id, nº de eventos, estado, latencia, error).
6. **Conciliación** (`GET /api/v1/admin/reconciliation`): compara saldo local y de Bancs cuenta por cuenta, con máximo
   50 cuentas y 2 consultas simultáneas (es la ÚNICA lectura hacia Bancs y es administrativa). Clasifica: `MATCH`,
   `PENDING_SYNC` (diferencia esperada: hay eventos sin enviar), `DISCREPANCY` (diferencia sin nada pendiente: real),
   `NOT_IN_BANCS`, `BANCS_ERROR`.

## Evidencia
- Con Bancs apagado: 40/40 transferencias OK, 40 eventos acumulados, circuito abierto, 0 eventos `DEAD`; al volver Bancs,
  atraso a 0 en ~24 s y conciliación `MATCH` (`evidence/bancs/resilience.txt`).
- Test de integración: una transferencia converge en Bancs sola; un cambio manual sin evento se detecta como
  `DISCREPANCY` (`tests/integration/test_bancs_sync.py`).

## Consecuencias
- (+) Bancs recibe una carga baja, predecible y acotada; su caída no afecta a las transferencias.
- (−) **Consistencia eventual:** entre la transferencia y su reflejo en Bancs pasan unos segundos (o el tiempo de una caída).
  El saldo local puede ir "por delante"; la conciliación lo hace observable.
- (−) La conciliación consulta cuenta por cuenta, por lo que no escala a millones de cuentas: en producción se haría con
  una extracción por lotes/archivo nocturno del legado.
- (−) El mock guarda el estado en memoria: un reinicio de `bancs-mock` reinicia los saldos a los del seed (el legado real es persistente).
