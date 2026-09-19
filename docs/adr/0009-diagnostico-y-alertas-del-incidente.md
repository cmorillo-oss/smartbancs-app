# ADR 0009 — Diagnóstico e incidentes: pool aparte, `/ready` que detecta degradación, alertas probadas

- **Estado:** Aceptado · **Fase:** 7

## Contexto
El enunciado (3.5) describe un incidente de quincena: transferencias que no se completan, latencia severa, **timeouts
de conexión con la BD** y **posibles deadlocks**. Se pide reproducirlo, detectarlo y que el operador identifique el
cuello de botella en segundos. Se reprodujeron dos causas reales (`tests/incident/`).

## Decisiones
1. **Deadlocks: el orden determinista (ADR 0003) es la solución; aquí se demuestra.** Con orden invertido PostgreSQL abortó
   29 de 30 transferencias cruzadas (SQLSTATE 40P01, ~1 s de espera cada una); con orden por id, 0 deadlocks en las mismas 30.
   La cifra sale de `pg_stat_database` (PostgreSQL), no del script.
2. **Pool agotado: se reproduce con una sola sesión.** Una transacción larga retiene una fila; las transferencias que la necesitan
   esperan el bloqueo *sin soltar su conexión* y en segundos el pool (30) se agota: en el ensayo, hasta ~170-200 peticiones en cola.
   Cliente: 100 % de 503 (`LOCK_TIMEOUT` y `POOL_TIMEOUT`). Una causa mínima paraliza todo: por eso hay `lock_timeout` y `pool_timeout`
   (fallar rápido) y por eso se mide.
3. **Métrica `smartbancs_db_pool_connections{state="waiting"}`** = peticiones esperando conexión. SQLAlchemy no la expone; se envuelve
   el punto donde el pool entrega conexiones. Es la señal directa del agotamiento (`checked_out` al máximo solo dice que está lleno).
4. **`/ready` con plazo de 1 s**: si no consigue conexión, 503 `pool_exhausted` (y logs con el estado del pool). El balanceador saca la
   instancia de rotación sin matarla. `/health` sigue sin tocar la BD (ADR 0001). Detección medida: 1-3 s desde el inicio del incidente.
5. **Diagnóstico con pool propio (2 conexiones reservadas, `statement_timeout` 5 s).** Si usara el pool principal, se quedaría esperando
   justo cuando el operador lo necesita. `/diagnostics/{pool, locks, blocking-tree, slow-queries}`; `blocking-tree` identificó al culpable
   (sesión, aplicación, su consulta y a cuántas sesiones bloquea) a **1 s** del inicio, con una consulta de 68-554 ms.
6. **Alertas con prueba unitaria (`promtool test rules`, 9 casos)**: p95 > 2 s, errores técnicos > 1 % (excluye los de negocio),
   deadlocks > 0, pool agotado, outbox atascado (cola grande **que no baja**: una cola que se vacía es una recuperación normal), circuitos abiertos.

## Lecciones aprendidas al reproducir el incidente (fallos míos, encontrados al probar de verdad)
- **El endpoint de diagnóstico casi tumba la API.** La primera versión de `blocking-tree` recorría recursivamente el grafo de bloqueos.
  Cuando N sesiones hacen cola, Postgres informa a cada una como bloqueada por el culpable *y por todas las anteriores* (grafo denso):
  los caminos crecen como 2^N y el endpoint consumió **2.5 GB de RAM y congeló la API** durante el incidente. Corregido con un árbol de
  expansión lineal (`blocking_tree.py`) y un test de regresión con una cola de 200 sesiones. Moraleja: las herramientas de diagnóstico se
  prueban bajo el incidente real, no solo en reposo.
- **Una alerta correcta en papel no disparaba.** `waiting > 0` con `for: 15s` se reiniciaba porque la cola oscila entre oleadas. Se usó
  `max_over_time(...[30s])`; el test unitario incluye ahora una serie oscilante. Además, Prometheus **no recarga reglas** al recrear el
  contenedor: hay que reiniciarlo tras cambiarlas (la demo corrió con la regla vieja hasta darme cuenta).
- **Los clientes de prueba también se saturan**: uno solo para carga y muestreo se bloqueaba a sí mismo; se separó (como en la realidad, donde el
  operador tiene su propia conexión).

## Consecuencias
- (+) Detección en segundos y culpable identificado con datos; alertas verificadas contra series sintéticas y contra el incidente real.
- (−) Los endpoints admin no tienen autenticación (fuera del alcance del reto): en producción irían tras autenticación y red interna.
- (−) La métrica `waiting` cuenta también, brevemente, a las peticiones que consiguen conexión al instante (microsegundos); bajo saturación domina la cola real.
- (−) `deadlocks_total` de la API solo cuenta los que ve la API; para deadlocks de cualquier cliente haría falta `postgres_exporter`.
