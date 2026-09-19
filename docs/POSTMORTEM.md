# Post mortem (sección 3.6)

Este documento analiza **dos incidentes**:

- **Incidente 1 — El del enunciado (simulado):** pico de quincena con transferencias que no se completan, latencia severa, timeouts de conexión a la BD y deadlocks. Se reprodujo con datos reales en la Fase 7.
- **Incidente 2 — El real (encontrado al probar):** el endpoint de diagnóstico `blocking-tree` consumió **2,5 GB de RAM** y congeló la API justo durante el Incidente 1.

El formato es *blameless*: se analiza el sistema, no a las personas. Es un post mortem de un **ejercicio** (no hubo clientes reales); lo que sí es real son las mediciones.

> **Cómo leer las marcas.** Los campos con **[COMPLETAR]** son datos que el repo no puede saber (horas exactas, nombres de responsables, fechas comprometidas). Las cifras sin marca salen de `evidence/` y del historial de git.

---

# INCIDENTE 1 — Pico de quincena: deadlocks y agotamiento del pool

## 1. Resumen

Durante un pico transaccional simulado, las transferencias dejaron de completarse. Se reprodujeron **dos causas técnicas independientes** que producen los síntomas del enunciado:

1. **Deadlocks** cuando dos transferencias cruzadas (A→B y B→A) bloquean las cuentas en orden inverso: PostgreSQL abortó **29 de 30** (`evidence/incident/deadlock_demo.txt`).
2. **Agotamiento del pool de conexiones** cuando una sola sesión retiene una fila y las demás transferencias esperan sin soltar su conexión: en segundos las **30 conexiones** se ocuparon, hubo hasta **174 peticiones en cola** y el **100 %** de las transferencias falló con 503 (`evidence/incident/exhaust_pool.txt`).

## 2. Impacto

| Dimensión | Deadlocks (orden invertido) | Pool agotado |
|---|---|---|
| Usuarios afectados | Simulado: 30 transferencias lanzadas → **29 fallidas (97 %)** | Simulado: **100 %** de las transferencias durante el incidente (1.203 con `LOCK_TIMEOUT` = 74,0 % y 422 con `POOL_TIMEOUT` = 26,0 %; 1.625 en total) |
| Duración | Cada víctima esperó ~1,1 s hasta el error (`deadlock_timeout` = 1 s); demo total 2,1 s | ~44 s de ensayo con el bloqueo retenido; recuperación inmediata al liberarlo |
| Latencia | – | `/ready` tardó 0,9-2,7 s en responder durante el incidente |
| **Monto perdido o duplicado** | **0 por diseño:** las transferencias abortadas hacen *rollback*. (La demo de deadlocks usa cuentas propias y no audita saldos; lo que sí está medido es la conservación del dinero en `tests/concurrency/`.) | **0 por diseño**, ídem. La corrida no auditó saldos. |
| Clientes / dinero real | Ninguno (entorno de pruebas) | Ninguno |

> La ausencia de pérdida de dinero no es suerte: es consecuencia de tener una sola transacción atómica por transferencia y de `CHECK (balance >= 0)` en la BD.

## 3. Línea de tiempo

### 3.1 Pool agotado (medido, segundos desde el inicio)

| t | Evento |
|---|---|
| 0 s | Estado sano: `/ready` = 200, capacidad 30 conexiones. Una sesión (pid 4200) abre una transacción, bloquea una cuenta y **no la termina**. |
| 1,0 s | Pool 30/30 en uso, 20 en cola. `/ready` → 503 `pool_exhausted` (**detección**). `/diagnostics/blocking-tree` identifica a la sesión culpable, `idle in transaction`, que bloquea a 30 sesiones (**diagnóstico**). |
| 3,9 s | 120 peticiones en cola. |
| 7,1 s | 174 peticiones en cola (pico). Alerta `DatabasePoolExhausted` → *pending*. |
| 14,6 s | `TransferErrorRateHigh` y `TransferLatencyP95High` → *pending*. |
| 22,2 s | `DatabasePoolExhausted` → **firing**. |
| 43,6 s | `TransferErrorRateHigh` y `TransferLatencyP95High` → **firing**. |
| fin | Se libera el bloqueo: `/ready` = 200 y sin cola de inmediato (**resolución**). |

### 3.2 Deadlocks (medido)

| t | Evento |
|---|---|
| 0 s | 30 transferencias cruzadas A→B / B→A lanzadas a la vez con orden de bloqueo invertido. |
| ~1,0 s | PostgreSQL detecta el ciclo (`deadlock_timeout` = 1 s) y aborta a las víctimas con SQLSTATE `40P01`. |
| 2,1 s | Terminó la demo: 1 completada, 29 abortadas. |
| – | Misma carga con orden por `account_id`: 30 completadas, 0 deadlocks en 1,78 s. |

**[COMPLETAR]** Si el jurado pide horas de reloj, agregar aquí la fecha y hora reales de la corrida (`evidence/incident/postgres_log_deadlock.txt` registra `2026-09-19 02:58:08 UTC`).

## 4. Causa raíz — los 5 porqués

**Síntoma: las transferencias no se completan y la latencia se dispara.**

*Cadena A (deadlocks):*
1. **¿Por qué fallan las transferencias?** PostgreSQL aborta una de cada par con `deadlock detected`.
2. **¿Por qué hay deadlocks?** Dos transferencias cruzadas se esperan en círculo: cada una tiene la cuenta que la otra necesita.
3. **¿Por qué se esperan en círculo?** Cada una bloquea primero su cuenta **origen** y luego la **destino**, o sea en orden distinto según la dirección.
4. **¿Por qué se bloquea en ese orden?** Es la implementación "natural" (leer origen, leer destino), y nada en el diseño obligaba a un orden común.
5. **¿Por qué nada lo obligaba?** El orden de bloqueo es una **invariante implícita**: no estaba escrita en un contrato ni verificada por un test.

**Causa raíz A:** *falta de un orden global de adquisición de recursos.* **Ya corregido** en el diseño (ADR 0003: `ORDER BY id FOR UPDATE`), y demostrado (29 → 0).

*Cadena B (pool agotado):*
1. **¿Por qué falla el 100 % de las transferencias?** Ninguna consigue una conexión del pool (o su bloqueo) a tiempo: 503.
2. **¿Por qué no hay conexiones?** Las 30 están ocupadas por peticiones que **esperan un bloqueo de fila**, sin soltar su conexión.
3. **¿Por qué esperan tanto?** Una sesión retiene la fila y su transacción no termina (`idle in transaction`).
4. **¿Por qué una sola sesión paraliza todo?** El pool es un recurso **compartido y pequeño**: quien espera lo consume, aunque no haga trabajo, y toda petición nueva hace cola detrás.
5. **¿Por qué el sistema no lo contiene antes?** No había un tope efectivo al *tiempo que una transacción puede retener recursos* ni un límite de admisión: los timeouts (`lock_timeout` 3 s, `pool_timeout` 5 s) hacen que fallen, pero no evitan que la cola se llene mientras tanto.

**Causa raíz B:** *un recurso compartido finito (el pool) sin aislamiento entre tipos de carga y sin tope al tiempo de retención de bloqueos por sesiones ajenas.* **Parcialmente mitigado** (fallo rápido, detección y diagnóstico); la prevención completa está en las acciones de la sección 10.

## 5. Detección

| Señal | Qué tan rápido |
|---|---|
| `/ready` devuelve 503 `pool_exhausted` | **1,0 s** |
| Métrica `smartbancs_db_pool_connections{state="waiting"}` sube | inmediata (pico 174) |
| Alerta `DatabasePoolExhausted` | pending 7,1 s · firing 22,2 s |
| Alertas de latencia y de tasa de error | pending 14,6 s · firing 43,6 s |
| Deadlocks | `smartbancs_db_deadlocks_total` (API) y log de PostgreSQL con las 4 consultas del ciclo |

**Detección de mala calidad, por honestidad:** las alertas tardaron entre 22 y 44 s en *firing* por sus ventanas `for:`; eso es aceptable para una alerta que despierta a una persona, pero **`/ready` fue 20 veces más rápido**. Y las alertas **no notifican a nadie** (no hay Alertmanager).

## 6. Resolución

1. Identificar la sesión culpable con `/diagnostics/blocking-tree` (pid, aplicación, última consulta, a cuántas bloquea).
2. Liberar el bloqueo (`pg_terminate_backend(<pid>)` o terminar el proceso cliente).
3. El sistema se recupera solo: `/ready` vuelve a 200 y la cola desaparece de inmediato; el outbox procesa su atraso sin intervención.

Para los deadlocks la resolución es de diseño (orden determinista); el reintento con backoff + jitter cubre el caso residual.

## 7. Qué salió bien

- **Sin pérdida de dinero por diseño** (atomicidad + `CHECK` en la BD), respaldado por las pruebas de concurrencia; en estas dos corridas no se auditaron saldos.
- El fallo fue **rápido y explícito** (503 con `error_code`), no un cuelgue.
- `/ready` detectó la degradación en 1 s y el `blocking-tree` **nombró al culpable** en 1 s.
- El diagnóstico con **pool propio** funcionó cuando el pool principal estaba agotado.
- El orden determinista eliminó los deadlocks (29 → 0), medido en PostgreSQL y no en el script.
- Cada caso quedó como evidencia reproducible (`make deadlock-demo`, `make exhaust-pool`).

## 8. Qué salió mal

- La alerta `DatabasePoolExhausted` **no disparaba en su primera versión** (ver Incidente 2 / lecciones): `waiting > 0` con `for: 15s` se reiniciaba porque la cola oscila entre oleadas.
- Prometheus **no recarga las reglas** al recrear su contenedor: la demo corrió con la regla vieja hasta darnos cuenta.
- El cliente de pruebas se saturaba a sí mismo (usaba el mismo cliente para generar carga y para muestrear); hubo que separarlos.
- Las lecturas (`GET`) devolvían **500** en lugar de 503 ante `POOL_TIMEOUT` (corregido en la Fase 7).
- La tasa de error subió a 46 % con 300 usuarios: el sistema **se degrada** en vez de rechazar limpiamente el exceso (no hay control de admisión).

## 9. Dónde tuvimos suerte

- **El incidente ocurrió en un ensayo**, con el operador mirando. En producción, con la métrica `waiting` sin alerta que notifique (no hay Alertmanager), habría dependido de que alguien mirara Prometheus.
- **La sesión culpable era una sola y visible** (`idle in transaction` con el nombre de aplicación `incident-simulator`). Un bloqueo causado por muchos clientes o por una consulta larga habría sido más difícil de aislar.
- El Incidente 2 (abajo) **casi hace inútil el diagnóstico** justo cuando se necesitaba, y solo lo vimos porque se probó bajo el incidente real.

## 10. Acciones preventivas

Responsable y fecha son **propuestas**: la autora debe confirmarlos o cambiarlos.

### 10.1 De infraestructura

| # | Acción | Responsable | Fecha objetivo | Estado |
|---|---|---|---|---|
| I-1 | **Alertmanager** (o equivalente) para que las alertas lleguen a una persona (correo/chat/paginador) | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-2 | `idle_in_transaction_session_timeout` (p. ej. 10-30 s) y `statement_timeout` en PostgreSQL: mata solo a las sesiones que retienen bloqueos sin trabajar | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-3 | **PgBouncer** delante de PostgreSQL y separar pools por tipo de carga (transferencias / lecturas / admin) para que una causa no paralice todo | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-4 | Dashboards de Grafana con las señales RED + pool + outbox, y exportar trazas a un colector OpenTelemetry | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-5 | `postgres_exporter` para ver deadlocks y bloqueos de **cualquier** cliente | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-6 | Réplica de PostgreSQL y respaldos con recuperación a un punto en el tiempo | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-7 | Prueba de carga periódica (antes de cada quincena) con `make load`; guardar `verification.txt` | [COMPLETAR] | [COMPLETAR] | Pendiente |

### 10.2 De código

| # | Acción | Responsable | Fecha objetivo | Estado |
|---|---|---|---|---|
| C-1 | Orden determinista de bloqueo (`ORDER BY id FOR UPDATE`) | Autora | 2026-09-18 | ✅ Hecho (ADR 0003) |
| C-2 | `lock_timeout` de 3 s y `pool_timeout` de 5 s (fallar rápido) | Autora | 2026-09-18 | ✅ Hecho |
| C-3 | Reintento ante deadlock con backoff exponencial + jitter (3 intentos) | Autora | 2026-09-18 | ✅ Hecho |
| C-4 | `/ready` con plazo de 1 s que detecta el pool agotado | Autora | 2026-09-18 | ✅ Hecho |
| C-5 | Pool propio y `statement_timeout` para los endpoints de diagnóstico | Autora | 2026-09-18 | ✅ Hecho |
| C-6 | Alertas con prueba unitaria (`promtool test rules`) | Autora | 2026-09-18 | ✅ Hecho |
| C-7 | **Control de admisión / *load shedding*:** rechazar rápido con 429/503 cuando `waiting` supere un umbral, en lugar de dejar que la cola crezca | [COMPLETAR] | [COMPLETAR] | Pendiente |
| C-8 | **Test automático del orden de bloqueo** que falle si alguien vuelve a bloquear en orden distinto (la invariante deja de ser implícita) | [COMPLETAR] | [COMPLETAR] | Pendiente |
| C-9 | Autenticación en los endpoints `admin` | [COMPLETAR] | [COMPLETAR] | Pendiente |
| C-10 | Repetir `make load` tras el cambio de 500 → 503 en lecturas y guardar la evidencia | [COMPLETAR] | [COMPLETAR] | Pendiente |

---

# INCIDENTE 2 — El endpoint `blocking-tree` consumió 2,5 GB y congeló la API (incidente real)

> Este sí fue un incidente **real** del proyecto (aunque en el entorno de desarrollo): ocurrió al ejecutar el Incidente 1 y **lo causó una herramienta creada para diagnosticar**.

## 1. Resumen

Al ejecutar `tests/incident/exhaust_pool.py` por primera vez con la primera versión de `GET /api/v1/admin/diagnostics/blocking-tree`, el proceso de la API pasó a consumir **2,5 GB de RAM** y quedó **congelado**. Es decir, la herramienta que debía ayudar a diagnosticar el incidente **empeoró el incidente**. Se corrigió en el mismo día (Fase 7).

## 2. Impacto

| Dimensión | Detalle |
|---|---|
| Usuarios | Ninguno real. En producción habría sido una **segunda caída encima de la primera**: la API entera congelada, no solo las transferencias. |
| Recursos | **~2,5 GB de RAM** en un contenedor de una máquina con 3,8 GB para Docker: al borde de matar otros contenedores por falta de memoria. |
| Duración | **[COMPLETAR]** hora de inicio y de resolución (el commit de la corrección es del 2026-09-18, `916a8ff`). |
| Dinero | 0 |

## 3. Línea de tiempo

Los hechos confirmados salen del commit `916a8ff` y del ADR 0009 (versión recursiva → 2,5 GB y API congelada durante el incidente → árbol lineal + test de 200 sesiones). Los detalles marcados con **[CONFIRMAR]** son mi reconstrucción y la autora debe corregirlos con lo que recuerde; las **horas exactas no se guardaron**.

| Orden | Evento |
|---|---|
| 1 | Se escribe `blocking-tree` como recorrido **recursivo** del grafo de bloqueos. **[CONFIRMAR]** Solo se había probado con casos pequeños o en reposo. |
| 2 | Se ejecuta el incidente (`exhaust_pool.py`): decenas de sesiones hacen cola detrás de una y se consulta el endpoint. |
| 3 | El endpoint **consume 2,5 GB de RAM y congela la API**. |
| 4 | Se identifica la causa (grafo denso → explosión de caminos) (ADR 0009). |
| 5 | Se reescribe como **árbol de expansión lineal** (`blocking_tree.py`) y se añade un test de regresión con una cola de **200 sesiones** (`tests/unit/test_blocking_tree.py`). |
| 6 | Se vuelve a ejecutar el incidente: `blocking-tree` responde y nombra al culpable en 1,0 s (consulta de 68-554 ms). |

## 4. Causa raíz — los 5 porqués

1. **¿Por qué se congeló la API?** El endpoint consumió 2,5 GB de RAM y CPU calculando caminos.
2. **¿Por qué consumió tanto?** Recorría **recursivamente** el grafo "quién bloquea a quién" enumerando todos los caminos.
3. **¿Por qué eso es explosivo?** Cuando N sesiones hacen cola, PostgreSQL informa a cada una como bloqueada por el culpable **y por todas las anteriores**: el grafo es *denso* (≈ N²/2 aristas) y el número de caminos crece como **2^N**.
4. **¿Por qué no se vio antes?** La explosión solo aparece con una cola de muchas sesiones, es decir, **solo durante el incidente real**; una consulta en reposo o con pocas sesiones no la muestra (ADR 0009: "las herramientas de diagnóstico se prueban bajo el incidente real, no solo en reposo").
5. **¿Por qué se probó así?** No existía el hábito de probar las herramientas de diagnóstico **bajo carga y con datos de tamaño de incidente**; se asumió que una consulta "de solo lectura" es inofensiva.

**Causa raíz:** *una herramienta de diagnóstico sin acotar su coste y no probada bajo condiciones de incidente.*

## 5. Detección

**[CONFIRMAR]** Se notó a mano mientras se ejecutaba el ensayo (la API dejó de responder). **No pudo haber alerta**: no existe ninguna sobre memoria ni CPU de la API. Esto es una debilidad, y está en las acciones preventivas.

## 6. Resolución

- **Corrección:** reescritura como **árbol de expansión** (cada sesión se cuelga de un solo bloqueador), con coste **lineal** en el número de sesiones (`app/services/blocking_tree.py`).
- **Test de regresión:** `test_cola_densa_de_200_sesiones_es_lineal_y_rapida` (200 sesiones en cola) y `test_deadlock_en_curso_se_reporta_como_ciclo_sin_colgarse` (un ciclo real no debe colgar el recorrido).
- **Contención adicional (ya existía):** el endpoint usa un pool propio y `statement_timeout` de 5 s.

## 7. Qué salió bien

- El fallo se encontró **en un ensayo** y no en una quincena real.
- La corrección fue **pequeña y con un test que impide que vuelva** (200 sesiones; un cambio que reintroduzca la explosión hará fallar el test).
- El aislamiento (pool propio del diagnóstico) evitó que además se consumieran las conexiones de las transferencias.

## 8. Qué salió mal

- La primera versión no tenía **ningún límite** (ni de profundidad, ni de nodos, ni de memoria).
- **No hubo alerta de memoria**: se descubrió porque alguien estaba mirando.
- La API y el diagnóstico **comparten proceso**: una falla del diagnóstico tumba la API. Idealmente el diagnóstico correría aislado (otro proceso o consultas hechas por la propia BD).

## 9. Dónde tuvimos suerte

- Ocurrió con una cola de **decenas de sesiones**. Con bastantes más, la explosión habría agotado la RAM del contenedor (y quizá la de todo Docker Desktop) antes de poder observarla.
- La máquina tenía 3,8 GB para Docker: **2,5 GB cabían**. En un contenedor con límite de memoria, el sistema operativo habría matado el proceso (OOM), lo que al menos habría sido un fallo limpio; sin límite, pudo llevarse consigo a otros contenedores.

## 10. Acciones preventivas

Responsable y fecha son **propuestas**: la autora debe confirmarlos.

### 10.1 De infraestructura

| # | Acción | Responsable | Fecha objetivo | Estado |
|---|---|---|---|---|
| I-8 | **Límites de memoria y CPU** por contenedor en el compose (`mem_limit`, `cpus`): un contenedor descontrolado se reinicia solo, sin arrastrar a los demás | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-9 | Alerta sobre memoria y CPU del contenedor de la API (cAdvisor / `docker stats` → Prometheus) | [COMPLETAR] | [COMPLETAR] | Pendiente |
| I-10 | Servir los endpoints `admin` desde un **proceso/instancia aparte**, para que una falla del diagnóstico no tumbe a las transferencias | [COMPLETAR] | [COMPLETAR] | Pendiente |

### 10.2 De código

| # | Acción | Responsable | Fecha objetivo | Estado |
|---|---|---|---|---|
| C-11 | Reescribir `blocking-tree` como árbol de expansión lineal | Autora | 2026-09-18 | ✅ Hecho |
| C-12 | Test de regresión con 200 sesiones en cola y con un ciclo | Autora | 2026-09-18 | ✅ Hecho |
| C-13 | **Cotas explícitas** en todo endpoint de diagnóstico: máximo de nodos/filas devueltos y presupuesto de tiempo, para que ninguno pueda crecer sin límite | [COMPLETAR] | [COMPLETAR] | Pendiente |
| C-14 | **Regla de equipo:** toda herramienta de diagnóstico se prueba con datos del tamaño del incidente (y se agrega a la lista de verificación de la PR) | [COMPLETAR] | [COMPLETAR] | Pendiente |

---

## Lecciones que se llevan a la defensa

1. **El orden de bloqueo es una invariante crítica; hay que probarla, no darla por supuesta** (Incidente 1).
2. **Un recurso compartido y pequeño (el pool) convierte una falla local en una caída total.** Hay que fallar rápido, aislar y limitar la admisión (Incidente 1).
3. **Las herramientas de diagnóstico se prueban bajo el incidente real, no en reposo** (Incidente 2).
4. **Una alerta correcta en papel puede no disparar**; hay que probarla contra series parecidas a las reales y reiniciar Prometheus al cambiar reglas.
5. **Medir antes de afirmar:** varias de estas cosas se descubrieron solo por ejecutar el incidente de verdad.
