# BRIEF DE CONSTRUCCIÓN — SmartBancs App (Reto Técnico TCS)

> **Para Claude Code.** Este documento es la especificación completa del proyecto.
> Está dividido en FASES. **Ejecuta UNA fase por vez y detente al final de cada una**
> para que el desarrollador verifique. No adelantes fases. No implementes nada que no
> esté en este brief sin preguntar primero.

---

## 0. CONTEXTO Y REGLAS DE ORO

### El reto
Institución financiera necesita "SmartBancs App": plataforma de transacciones en tiempo real
con recomendaciones financieras por IA.

**Tres restricciones que justifican TODA decisión técnica:**
1. **Alta concurrencia** — picos de hasta 10.000 transacciones por segundo
2. **Core legado "Bancs"** — sistema transaccional heredado que se degrada si recibe alto
   volumen de consultas directas. NUNCA se consulta en el camino crítico.
3. **Latencia** — transferencias en < 2 segundos. La IA **no puede** bloquear ni retrasar
   el flujo transaccional principal.

### Reglas de oro (NO NEGOCIABLES)

1. **Nunca llames a un servicio externo (IA o Bancs) dentro de una transacción de base de datos.**
2. **Nunca llames a la IA de forma bloqueante en el camino de la transferencia.** La respuesta
   HTTP al cliente no debe esperar a la IA jamás.
3. **Siempre adquiere bloqueos de cuentas en orden determinista** (`account_id` ascendente).
   Esta es la decisión arquitectónica central del proyecto — previene deadlocks y es la
   columna vertebral de la defensa técnica.
4. **Todo log es JSON estructurado y lleva `trace_id`.** Sin excepciones.
5. **Toda operación de escritura de dinero es idempotente** vía `idempotency_key`.
6. **Escribe comentarios en español explicando el PORQUÉ**, no el qué. El desarrollador tiene
   que defender este código oralmente ante un jurado el miércoles.
7. **Cada decisión técnica relevante genera un ADR** en `docs/adr/` en el momento de tomarla.
8. **Todo debe levantarse con UN SOLO COMANDO**: `docker compose up`. Es requisito literal del reto.

### Stack fijado (no cambiar)
- Python 3.11, FastAPI, Uvicorn
- SQLAlchemy 2.0 (async) + asyncpg
- PostgreSQL 16
- Pydantic v2
- structlog (logs JSON)
- prometheus-client (métricas)
- OpenTelemetry (trazas)
- Docker + Docker Compose
- pytest + httpx (tests)
- Locust o k6 (carga)

---

## 1. ESTRUCTURA DEL REPOSITORIO

```
smartbancs-app/
├── README.md
├── AI_USAGE_DECLARATION.md
├── docker-compose.yml
├── .env.example
├── Makefile
├── docs/
│   ├── DOCUMENTO_TECNICO.md
│   ├── adr/
│   ├── POSTMORTEM.md
│   └── diagrams/
├── services/
│   ├── transaction-api/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py
│   │       ├── config.py
│   │       ├── database.py
│   │       ├── models.py
│   │       ├── schemas.py
│   │       ├── observability/
│   │       │   ├── logging.py
│   │       │   ├── metrics.py
│   │       │   └── tracing.py
│   │       ├── repositories/
│   │       │   └── account_repository.py
│   │       ├── services/
│   │       │   ├── transfer_service.py
│   │       │   ├── ai_client.py
│   │       │   └── circuit_breaker.py
│   │       ├── workers/
│   │       │   └── outbox_worker.py
│   │       └── api/
│   │           └── routes.py
│   ├── ai-service/
│   └── bancs-mock/
├── database/
│   ├── ddl/01_schema.sql
│   ├── ddl/02_indexes.sql
│   └── dml/03_seed.sql
├── etl/
│   ├── transform.py
│   └── data/raw/raw_transactions.csv
├── observability/
│   ├── prometheus/prometheus.yml
│   └── grafana/
├── tests/
│   ├── concurrency/test_race_conditions.py
│   ├── incident/reproduce_deadlock.py
│   ├── load/locustfile.py
│   └── integration/
└── evidence/
    ├── test-data/
    └── load-test-results/
```

---

## FASE 1 — Fundaciones e Infraestructura
**Objetivo: `docker compose up` levanta Postgres + API con `/health` respondiendo.**

### Tareas
1. Crear estructura de carpetas completa (con `.gitkeep` donde haga falta).
2. `database/ddl/01_schema.sql` con este esquema exacto:

```sql
-- Cuentas: el saldo local es "saldo disponible" = saldo Bancs sincronizado - reservas locales
CREATE TABLE accounts (
    id              BIGSERIAL PRIMARY KEY,
    account_number  VARCHAR(20)  NOT NULL UNIQUE,
    customer_id     VARCHAR(36)  NOT NULL,
    balance         NUMERIC(18,2) NOT NULL DEFAULT 0,
    currency        CHAR(3)      NOT NULL DEFAULT 'USD',
    status          VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    version         INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Última línea de defensa: aunque falle la lógica, la BD no permite saldo negativo
    CONSTRAINT chk_balance_non_negative CHECK (balance >= 0),
    CONSTRAINT chk_status CHECK (status IN ('ACTIVE','BLOCKED','CLOSED'))
);

CREATE TABLE transactions (
    id                UUID PRIMARY KEY,
    idempotency_key   VARCHAR(64) NOT NULL UNIQUE,  -- garantiza no-duplicación
    source_account_id BIGINT      NOT NULL REFERENCES accounts(id),
    dest_account_id   BIGINT      NOT NULL REFERENCES accounts(id),
    amount            NUMERIC(18,2) NOT NULL,
    currency          CHAR(3)     NOT NULL,
    status            VARCHAR(20) NOT NULL,
    trace_id          VARCHAR(64),
    error_code        VARCHAR(50),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    CONSTRAINT chk_amount_positive CHECK (amount > 0),
    CONSTRAINT chk_different_accounts CHECK (source_account_id <> dest_account_id),
    CONSTRAINT chk_tx_status CHECK (status IN ('PENDING','COMPLETED','FAILED','REVERSED'))
);

-- Partida doble: auditabilidad total, requisito de facto en banca
CREATE TABLE ledger_entries (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id UUID        NOT NULL REFERENCES transactions(id),
    account_id     BIGINT      NOT NULL REFERENCES accounts(id),
    entry_type     VARCHAR(10) NOT NULL,
    amount         NUMERIC(18,2) NOT NULL,
    balance_after  NUMERIC(18,2) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_entry_type CHECK (entry_type IN ('DEBIT','CREDIT'))
);

-- Patrón Outbox: desacopla la transacción de las integraciones (Bancs, IA)
CREATE TABLE outbox_events (
    id             BIGSERIAL PRIMARY KEY,
    aggregate_id   UUID        NOT NULL,
    event_type     VARCHAR(50) NOT NULL,
    payload        JSONB       NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    retry_count    INTEGER     NOT NULL DEFAULT 0,
    next_retry_at  TIMESTAMPTZ,
    trace_id       VARCHAR(64),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at   TIMESTAMPTZ,
    CONSTRAINT chk_outbox_status CHECK (status IN ('PENDING','PROCESSING','SENT','FAILED','DEAD'))
);

CREATE TABLE bancs_sync_log (
    id             BIGSERIAL PRIMARY KEY,
    batch_id       UUID        NOT NULL,
    events_count   INTEGER     NOT NULL,
    status         VARCHAR(20) NOT NULL,
    latency_ms     INTEGER,
    error_message  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE ai_recommendations (
    id             BIGSERIAL PRIMARY KEY,
    customer_id    VARCHAR(36) NOT NULL,
    transaction_id UUID REFERENCES transactions(id),
    recommendation JSONB       NOT NULL,
    model_version  VARCHAR(20) NOT NULL,
    latency_ms     INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

3. `database/ddl/02_indexes.sql`: índices en `outbox_events(status, next_retry_at)`,
   `transactions(source_account_id, created_at DESC)`, `transactions(status)`,
   `ledger_entries(account_id, created_at DESC)`, `ai_recommendations(customer_id)`.
   Comenta cada índice explicando qué consulta acelera.

4. `database/dml/03_seed.sql`: 20 cuentas de prueba con saldos conocidos.
   Incluir una cuenta con saldo exacto 1000.00 llamada `ACC-TEST-CONCURRENCY`
   (la usarán las pruebas de concurrencia).

5. `docker-compose.yml`: servicios `postgres` (con healthcheck y montaje de los .sql
   en `/docker-entrypoint-initdb.d/`), `transaction-api` (depends_on healthy).

6. `services/transaction-api/`: FastAPI mínimo con `/health` y `/ready`,
   conexión async a Postgres, Dockerfile multi-stage.

7. `Makefile` con: `up`, `down`, `logs`, `test`, `clean`, `seed`.

8. `.env.example` con todas las variables.

### ✅ Criterio de aceptación FASE 1
```bash
docker compose up -d
curl localhost:8000/health   # -> {"status":"ok"}
curl localhost:8000/ready    # -> {"status":"ready","database":"connected"}
docker compose down -v
```
**DETENTE AQUÍ. Reporta al desarrollador antes de continuar.**

---

## FASE 2 — Observabilidad (ANTES del endpoint, no después)
**Objetivo: infraestructura de logs, métricas y trazas lista para instrumentar.**

> ⚠️ Esta fase va ANTES del endpoint a propósito. Instrumentar mientras se escribe la
> lógica cuesta 30 minutos; retrofitearla después cuesta 3 horas y queda superficial.

### Tareas
1. `observability/logging.py` — structlog con salida JSON. Cada log incluye:
   `timestamp`, `level`, `event`, `service`, `trace_id`, `span_id`.
   Usar `contextvars` para propagar `trace_id` sin pasarlo por parámetro.

2. Middleware FastAPI que:
   - lee el header `X-Trace-Id` o genera un UUID4 si no viene
   - lo mete en el contextvar
   - lo devuelve en la respuesta como header
   - loguea inicio y fin de cada request con duración

3. `observability/metrics.py` — métricas Prometheus (nombres exactos):
   ```
   smartbancs_transactions_total{status, currency}          Counter
   smartbancs_transaction_duration_seconds{operation}       Histogram
     buckets: .005 .01 .025 .05 .1 .25 .5 1 2 5
   smartbancs_transaction_errors_total{error_code}          Counter
   smartbancs_db_query_duration_seconds{operation}          Histogram
   smartbancs_db_deadlocks_total                            Counter
   smartbancs_db_lock_wait_seconds                          Histogram
   smartbancs_db_pool_connections{state}                    Gauge
   smartbancs_ai_calls_total{status}                        Counter
   smartbancs_ai_call_duration_seconds                      Histogram
   smartbancs_ai_circuit_breaker_state                      Gauge
   smartbancs_outbox_pending_events                         Gauge
   smartbancs_bancs_sync_total{status}                      Counter
   ```
   Endpoint `/metrics`.

4. `observability/tracing.py` — OpenTelemetry con exportador OTLP,
   auto-instrumentación de FastAPI, SQLAlchemy y httpx.

5. Añadir Prometheus al compose con `observability/prometheus/prometheus.yml`.

6. ADR: `docs/adr/0001-observabilidad-primero.md` explicando por qué RED metrics
   (Rate, Errors, Duration) y por qué trace_id propagado.

### ✅ Criterio de aceptación FASE 2
`curl localhost:8000/metrics` muestra las métricas. Los logs salen en JSON con trace_id.
Prometheus en `localhost:9090` ve el target `transaction-api` como UP.

**DETENTE AQUÍ.**

---

## FASE 3 — El corazón: transferencia con control de concurrencia
**Objetivo: `POST /api/v1/transactions` correcto bajo concurrencia. Es el componente
de mayor peso de todo el reto.**

### Algoritmo EXACTO (implementar tal cual)

```
POST /api/v1/transactions
Header: Idempotency-Key: <uuid>   (obligatorio)
Body: { source_account, dest_account, amount, currency }

1. Validar payload (Pydantic). Monto > 0, cuentas distintas, divisa soportada.

2. Consultar idempotency_key en transactions.
   Si existe -> devolver el resultado almacenado con HTTP 200 y header
   `Idempotent-Replay: true`. NO reprocesar.

3. BEGIN TRANSACTION (READ COMMITTED)

4. ⭐ ORDEN DETERMINISTA DE BLOQUEO ⭐
   lock_ids = sorted([source_account_id, dest_account_id])
   SELECT * FROM accounts
     WHERE id = ANY(:lock_ids)
     ORDER BY id                  -- CRÍTICO: siempre ascendente
     FOR UPDATE;

   Esta línea es la decisión central del proyecto. Sin el ORDER BY, dos
   transferencias simultáneas A->B y B->A adquieren los bloqueos en orden
   inverso y producen un deadlock. Con el orden fijo, la segunda transacción
   simplemente espera. Documentar esto con un comentario extenso.

   Usar `FOR UPDATE NOWAIT` NO. Usar timeout de lock configurable:
   SET LOCAL lock_timeout = '3s';  -- falla rápido en vez de colgar el pool

5. Validar reglas de negocio con los datos YA bloqueados:
   - cuenta origen ACTIVE
   - cuenta destino ACTIVE
   - saldo suficiente
   - divisas coinciden

6. UPDATE accounts SET balance = balance - :amount, version = version + 1,
   updated_at = NOW() WHERE id = :source
   UPDATE accounts SET balance = balance + :amount, version = version + 1,
   updated_at = NOW() WHERE id = :dest

7. INSERT transactions (status='COMPLETED', trace_id)
   INSERT ledger_entries x2 (DEBIT origen, CREDIT destino, con balance_after)

8. INSERT outbox_events x2 EN LA MISMA TRANSACCIÓN:
   - 'bancs.balance_updated'  -> sincronización con el core legado
   - 'ai.transaction_created' -> alimenta las recomendaciones

   Esto es el patrón Outbox: el evento se persiste atómicamente con el cambio
   de saldo. Si el proceso muere aquí, al reiniciar el worker reenvía. Garantiza
   entrega at-least-once sin transacciones distribuidas ni 2PC contra Bancs.

9. COMMIT

10. DESPUÉS del commit, fuera de la transacción, sin await bloqueante:
    disparar notificación a la IA (fire-and-forget con BackgroundTasks).
    Si falla, se loguea y ya — el outbox es la red de seguridad.

11. Responder HTTP 201 con el resultado.
```

### Manejo de errores
Capturar `DeadlockDetected` (SQLSTATE 40P01):
- incrementar `smartbancs_db_deadlocks_total`
- loguear a nivel ERROR con: trace_id, ambas cuentas, la consulta, el tiempo de espera
- reintentar hasta 3 veces con backoff exponencial + jitter
- si agota reintentos -> HTTP 503 con error_code `DEADLOCK_RETRY_EXHAUSTED`

Capturar `LockNotAvailable` / timeout -> HTTP 503 `LOCK_TIMEOUT`.
Saldo insuficiente -> HTTP 422 `INSUFFICIENT_FUNDS`.

### Endpoints adicionales
- `GET /api/v1/accounts/{account_number}` — saldo y datos
- `GET /api/v1/accounts/{account_number}/transactions` — historial paginado
- `GET /api/v1/transactions/{id}` — detalle

### Pool de conexiones
Configurable por env: `DB_POOL_SIZE` (default 20), `DB_MAX_OVERFLOW` (default 10),
`DB_POOL_TIMEOUT`. Exponer el estado del pool en la métrica gauge.
Esto importa: el pool es el cuello de botella que se demostrará en la prueba de carga.

### ADRs de esta fase
- `0002-bloqueo-pesimista-vs-optimista.md` — por qué FOR UPDATE y no versioning optimista
  (en banca el conflicto es frecuente bajo contención; reintentar es peor que esperar)
- `0003-orden-determinista-de-bloqueos.md` — la decisión central
- `0004-patron-outbox.md` — por qué no 2PC contra Bancs
- `0005-idempotencia.md`

### ✅ Criterio de aceptación FASE 3
Transferencia exitosa persiste y cuadra. Reenviar la misma `Idempotency-Key` no duplica.
Saldo insuficiente devuelve 422 sin mover dinero.

**DETENTE AQUÍ.**

---

## FASE 4 — Prueba de concurrencia (la evidencia estrella)
**Objetivo: demostrar empíricamente que no hay race conditions.**

`tests/concurrency/test_race_conditions.py`:

1. **Test de sobregiro concurrente:** cuenta con 1000.00, lanzar 100 transferencias
   concurrentes de 50.00 cada una. Solo 20 deben tener éxito. El saldo final debe ser
   exactamente 0.00. Ni un centavo perdido ni creado.

2. **Test de suma cero:** 200 transferencias aleatorias entre 20 cuentas en paralelo.
   La suma total de saldos antes == después.

3. **Test de idempotencia bajo concurrencia:** 50 peticiones simultáneas con la MISMA
   idempotency_key. Debe crearse exactamente 1 transacción.

4. **Test de integridad del ledger:** suma de DEBIT == suma de CREDIT.

5. **Script comparativo `demo_race_condition.py`:** ejecuta la misma carga contra una
   implementación INSEGURA (sin FOR UPDATE, con read-modify-write) y muestra el dinero
   fantasma que aparece. Luego contra la segura. Salida lado a lado.
   **Esto va en el video.**

Guardar salidas en `evidence/test-data/`.

### ✅ Criterio de aceptación
`make test-concurrency` pasa en verde y el script comparativo muestra la diferencia.

**DETENTE AQUÍ.**

---

## FASE 5 — Servicio de IA y consumo no bloqueante
**Objetivo: demostrar que la IA nunca afecta el flujo transaccional.**

### `services/ai-service/` (contenedor independiente, puerto 8001)
- `POST /api/v1/recommendations` recibe `{customer_id, transaction_history, trace_id}`
- Motor de reglas que genera recomendaciones reales a partir de patrones de gasto
  (categorización, detección de gasto atípico, sugerencia de ahorro).
  No es un modelo entrenado — el reto permite explícitamente "un mock avanzado y funcional".
- **Latencia deliberada de 300-800ms** — es la justificación empírica de por qué no puede
  ser síncrono. Configurable por env `AI_SIMULATED_LATENCY_MS`.
- Endpoint `/health`, `/metrics`
- Modo de fallo inyectable: `AI_FAILURE_RATE` para demos de resiliencia
- Propaga el `trace_id` recibido en todos sus logs
- Expone `model_version` en cada respuesta

### Consumo desde transaction-api (`services/ai_client.py`)
- Cliente httpx **async** con timeout duro de 1s
- **Circuit breaker** propio: CLOSED -> OPEN tras 5 fallos consecutivos ->
  HALF_OPEN tras 30s. Exponer estado en métrica.
- La llamada ocurre en `BackgroundTasks` de FastAPI, **después del commit**
- Si el circuito está abierto: no llama, incrementa contador, sigue adelante
- Fallback: recomendación genérica cacheada

### `workers/outbox_worker.py`
Corre como proceso aparte en el compose. Cada 2 segundos:
1. `SELECT ... FROM outbox_events WHERE status='PENDING' AND (next_retry_at IS NULL OR next_retry_at <= NOW()) ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED`
   (el `SKIP LOCKED` permite escalar a N workers sin contención)
2. Agrupa por tipo, despacha en lote a Bancs o a IA
3. Backoff exponencial, máximo 5 reintentos, luego status `DEAD` (dead letter)
4. Actualiza gauge `outbox_pending_events`

### ADRs
- `0006-ia-asincrona-fuera-del-camino-critico.md`
- `0007-circuit-breaker.md`

### ✅ Criterio de aceptación FASE 5
**La prueba decisiva:** `docker compose stop ai-service`, luego lanzar 100 transferencias.
Todas deben completarse con la misma latencia p95 que antes. Medir y guardar ambos números
en `evidence/`. Este es el dato que responde "¿qué pasa si la IA se cae?".

**DETENTE AQUÍ.**

---

## FASE 6 — Bancs mock, sincronización y ETL
**Objetivo: cerrar el componente 3.2 del reto.**

### `services/bancs-mock/` (puerto 8002)
Simula el core legado con comportamiento realista:
- `POST /bancs/v1/accounts/balance/batch` — acepta lotes
- `GET /bancs/v1/accounts/{n}` — consulta individual
- **Latencia base 200-500ms**
- **Se degrada bajo carga:** si recibe > `BANCS_MAX_CONCURRENT` (default 10) peticiones
  concurrentes, la latencia crece linealmente y empieza a devolver 503.
  Esto hace visible por qué no se puede consultar en caliente.
- Registra su propio throughput en `/metrics`

### Sincronización (en el outbox worker)
- Agrupa eventos `bancs.balance_updated` en lotes de hasta 100
- Envía cada 2s o cuando el lote se llena
- Circuit breaker + backoff
- Registra cada lote en `bancs_sync_log`
- Endpoint `GET /api/v1/admin/reconciliation` que compara saldos locales vs Bancs
  y reporta discrepancias

### `etl/transform.py`
Pipeline de 4 etapas con logging por etapa y conteo de registros:

**EXTRACT** — lee `etl/data/raw/raw_transactions.csv`, que DEBE generarse con basura realista:
- valores nulos en campos críticos y no críticos
- fechas en 3 formatos distintos (`2024-01-15`, `15/01/2024`, `Jan 15, 2024`)
- montos como string con comas y símbolos (`"1,234.56"`, `$500.00`)
- divisas inconsistentes (`usd`, `USD`, `Dólares`)
- duplicados exactos y duplicados por clave de negocio
- espacios y mayúsculas inconsistentes
- filas corruptas con número de columnas incorrecto
- montos negativos y outliers

**CLEAN** — normalización de fechas a ISO-8601 UTC, montos a Decimal, divisas a ISO-4217,
trim y case, imputación documentada de nulos, deduplicación por clave de negocio,
cuarentena de filas irrecuperables en `etl/data/quarantine/` (no descartarlas en silencio).

**TRANSFORM** — enriquecimiento: categorización de transacciones, agregados por
cliente/día (total, promedio, conteo, desviación), flags de comportamiento atípico.
Formato de salida optimizado para consumo analítico y por la IA.

**LOAD** — escribe Parquet (columnar, justificar la elección) + carga a tabla analítica.

Reporte final: registros leídos, limpiados, cuarentenados, cargados + duración por etapa.

### ADR
- `0008-sincronizacion-bancs-sin-saturar-legado.md`

### ✅ Criterio de aceptación FASE 6
`make etl` corre end-to-end y produce el reporte. El worker sincroniza con Bancs
y `bancs_sync_log` tiene registros.

**DETENTE AQUÍ.**

---

## FASE 7 — Incidente simulado (el diferenciador)
**Objetivo: reproducir el incidente EXACTO del enunciado del reto y demostrar detección.**

> El enunciado describe: "pico transaccional de quincena, transferencias que no se completan,
> incremento severo de latencia, timeouts de conexión con la BD, y posibles deadlocks en
> las tablas principales". Vamos a reproducirlo literalmente.

### `tests/incident/reproduce_deadlock.py`
Provoca deadlocks REALES de PostgreSQL:
- Hilo A: transferencias cuenta1 -> cuenta2, bloqueando en orden 1,2
- Hilo B: transferencias cuenta2 -> cuenta1, bloqueando en orden 2,1 (ORDEN INVERTIDO)
- Ejecutar contra el modo inseguro -> Postgres detecta deadlocks (SQLSTATE 40P01)
- Capturar y mostrar: el error, ambas consultas implicadas, las cuentas, el trace_id,
  el tiempo de espera del bloqueo
- Ejecutar contra el modo seguro (orden determinista) -> **cero deadlocks**
- Salida comparativa lado a lado

### `tests/incident/exhaust_pool.py`
Satura el pool de conexiones para reproducir los timeouts descritos:
- Lanza más conexiones concurrentes que `DB_POOL_SIZE`
- Demuestra que la métrica `smartbancs_db_pool_connections{state="waiting"}` sube
- Demuestra que `/ready` detecta la degradación

### Endpoints de diagnóstico `GET /api/v1/admin/diagnostics/`
Para que el operador identifique el cuello de botella en segundos (requisito literal de 3.5):
- `/slow-queries` — consultas desde `pg_stat_statements` ordenadas por tiempo total
- `/locks` — bloqueos activos desde `pg_locks` + `pg_stat_activity`, con quién bloquea a quién
- `/pool` — estado del pool de conexiones
- `/blocking-tree` — árbol de sesiones bloqueantes

### `tests/load/locustfile.py`
Simula el pico de quincena:
- Rampa progresiva hasta saturación
- Mezcla realista: 70% transferencias, 20% consultas de saldo, 10% historial
- Reportar p50, p95, p99, TPS máximo sostenido, tasa de error
- Guardar resultados en `evidence/load-test-results/`

### Alertas Prometheus
`observability/prometheus/alerts.yml` con reglas para: latencia p95 > 2s,
tasa de error > 1%, deadlocks > 0, pool agotado, outbox atascado.

### ✅ Criterio de aceptación FASE 7
`make deadlock-demo` muestra deadlocks en modo inseguro y cero en modo seguro.
`make load` produce números reales de TPS.

**DETENTE AQUÍ.**

---

## FASE 8 — Documentación y entrega

### `README.md` (el archivo más leído del repo)
1. Descripción en 3 líneas
2. **Tabla de mapeo sección del reto -> archivo del repo** (crítico: permite al jurado
   verificar cobertura en 30 segundos)
3. Diagrama de arquitectura
4. Prerrequisitos (versiones exactas)
5. **Instalar / Ejecutar / Probar / DETENER** — las cuatro, el reto las pide literalmente
6. Tabla de endpoints
7. Comandos de demo (concurrencia, deadlock, carga, IA caída)
8. Resultados de las pruebas con números reales
9. Limitaciones conocidas y hoja de ruta

### `docs/DOCUMENTO_TECNICO.md`
Estructura obligatoria, una sección por punto del reto:
1. Resumen ejecutivo (1 página — es el guion de los 3 minutos de defensa)
2. Contexto y restricciones
3. Arquitectura general + diagramas
4. **3.1** Infraestructura, BD y backend — justificación del stack contra las 3 restricciones
5. **3.2** Integración Bancs — estrategia de sincronización + ETL
6. **3.3** IA — integración asíncrona + ciclo de vida del modelo (alimentación con datos
   nuevos, monitoreo de data drift con PSI/KS, gestión de recursos, reentrenamiento,
   versionado, rollback)
7. **3.4** Observabilidad — qué se instrumentó y qué información se usa para detectar
   degradación, con justificación de cada señal
8. **3.5** Incidente simulado — detección, diagnóstico, acciones inmediatas de estabilización
9. **3.6** Post mortem y acciones preventivas
10. Escalabilidad a 10.000 TPS: medición real + ruta de escalado con aritmética
11. **Limitaciones conocidas** (sección honesta, protege en el Q&A)

### `docs/POSTMORTEM.md` (sección 3.6)
Formato estándar: resumen, impacto (usuarios, duración, monto), línea de tiempo,
causa raíz con 5 porqués, detección, resolución, qué salió bien, qué salió mal,
dónde tuvimos suerte, acciones preventivas de infraestructura, acciones preventivas
de código, con responsable y fecha.

### `AI_USAGE_DECLARATION.md`
Plantilla honesta y detallada: herramientas usadas, en qué componentes, cómo se usaron,
qué se revisó y validó manualmente. Es entregable obligatorio del reto.

### Diagramas (Mermaid en los .md)
1. Arquitectura de componentes
2. Secuencia de una transferencia (mostrando el límite asíncrono de la IA)
3. Flujo de sincronización con Bancs
4. Pipeline ETL
5. Diagrama del deadlock (orden invertido vs. determinista)

---

## COMANDOS FINALES DEL MAKEFILE
```
make up               # levanta todo
make down             # detiene y limpia
make seed             # datos de prueba
make test             # todos los tests
make test-concurrency # prueba de race conditions
make demo-race        # comparativo inseguro vs seguro
make deadlock-demo    # reproduce el incidente de 3.5
make load             # prueba de carga
make etl              # pipeline de transformación
make ai-down          # apaga la IA para demostrar resiliencia
make diagnostics      # consulta los endpoints de diagnóstico
```

---

## RECORDATORIO FINAL PARA CLAUDE CODE

- Una fase por vez. Detente y reporta al terminar cada una.
- Comenta el PORQUÉ en español. El desarrollador defiende esto oralmente el miércoles.
- Si algo no está especificado aquí, pregunta antes de improvisar.
- Prioridad si falta tiempo: Fases 1, 2, 3, 4 son irrenunciables. 5 y 6 muy importantes.
  7 es el diferenciador. 8 es obligatoria (es la mitad de la nota).
