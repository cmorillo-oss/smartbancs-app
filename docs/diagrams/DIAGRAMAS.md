# Diagramas de SmartBancs

Todos están en [Mermaid](https://mermaid.js.org/): GitHub, GitLab y VS Code (con la extensión *Markdown Preview Mermaid*) los dibujan solos.
Los mismos diagramas aparecen dentro del `README.md` y del `docs/DOCUMENTO_TECNICO.md`.

---

## 1. Arquitectura de componentes

La regla que ordena todo el dibujo: **las flechas continuas son el camino crítico** (lo que espera el cliente) y **las punteadas son trabajo en segundo plano** (nadie espera por ellas).

```mermaid
flowchart LR
    Cliente["Cliente<br/>(app / Swagger / Locust)"]

    subgraph Critico["Camino critico (el cliente espera)"]
        API["transaction-api<br/>FastAPI :8000"]
        PG[("PostgreSQL 16<br/>cuentas, transacciones,<br/>ledger y OUTBOX")]
    end

    subgraph Segundo["Segundo plano (nadie espera)"]
        W["outbox-worker<br/>(proceso aparte)"]
        IA["ai-service :8001<br/>latencia 300-800 ms"]
        BANCS["bancs-mock :8002<br/>core legado simulado"]
    end

    subgraph Obs["Observabilidad"]
        PROM["Prometheus :9090<br/>metricas + alertas"]
    end

    ETL["ETL (make etl)<br/>CSV -> Parquet + tablas analiticas"]

    Cliente -->|"POST /transactions"| API
    API -->|"1 sola transaccion:<br/>saldos + ledger + outbox"| PG
    W -.->|"lee eventos PENDING<br/>FOR UPDATE SKIP LOCKED"| PG
    W -.->|"lotes de hasta 100"| BANCS
    W -.->|"1 evento por transaccion<br/>timeout 1 s + circuit breaker"| IA
    W -.->|"guarda recomendacion"| PG
    PROM -.->|"scrape /metrics"| API
    PROM -.->|"scrape /metrics"| IA
    PROM -.->|"scrape /metrics"| BANCS
    ETL -.->|"carga analitica"| PG

    API -. "NUNCA llama a Bancs ni a la IA<br/>dentro del camino critico" .- W
```

---

## 2. Secuencia de una transferencia (con el límite asíncrono de la IA)

Fíjate en la línea "**LÍMITE ASÍNCRONO**": todo lo de arriba ocurre antes de responder al cliente; todo lo de abajo ocurre después y puede fallar sin que el cliente lo note.

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant API as transaction-api
    participant DB as PostgreSQL
    participant W as outbox-worker
    participant B as Bancs (mock)
    participant IA as ai-service

    C->>API: POST /api/v1/transactions<br/>Idempotency-Key + payload
    API->>API: Valida payload (monto > 0, cuentas distintas, divisa)
    API->>DB: ¿existe esa idempotency_key?
    alt ya existe
        DB-->>API: resultado guardado
        API-->>C: 200 + Idempotent-Replay: true (no reprocesa)
    else es nueva
        API->>DB: BEGIN (READ COMMITTED) + lock_timeout 3 s
        API->>DB: SELECT ... WHERE id = ANY(...) ORDER BY id FOR UPDATE<br/>(bloquea en orden ascendente de id)
        API->>API: Reglas de negocio con las filas YA bloqueadas<br/>(ACTIVE, saldo suficiente, misma divisa)
        API->>DB: UPDATE saldo origen y destino
        API->>DB: INSERT transaccion + 2 asientos del ledger
        API->>DB: INSERT 2 eventos en outbox (Bancs e IA)
        API->>DB: COMMIT (todo o nada)
        API-->>C: 201 Created  (aprox. 200-800 ms medidos)
    end

    Note over API,IA: ════ LÍMITE ASÍNCRONO: desde aquí el cliente ya no espera ════

    loop cada 2 s
        W->>DB: reclama eventos PENDING (SKIP LOCKED)
        W->>B: lote de hasta 100 saldos (circuit breaker + backoff)
        B-->>W: OK / 503
        W->>IA: notifica transacción (timeout duro 1 s)
        IA-->>W: recomendación (300-800 ms) o fallo
        W->>DB: marca SENT, o reintenta con backoff, o DEAD tras 5 fallos
    end
    C->>API: GET /customers/{id}/recommendations
    API->>DB: lee la última recomendación guardada
    API-->>C: recomendación (o una genérica si aún no hay)
```

---

## 3. Flujo de sincronización con Bancs

```mermaid
flowchart TD
    T["Transferencia confirmada<br/>(COMMIT)"] --> E["Evento bancs.balance_updated<br/>en la tabla outbox_events<br/>(saldo ABSOLUTO + sequence)"]
    E --> Q{"Worker: cada 2 s<br/>o lote lleno (100)"}
    Q --> CB{"¿Circuit breaker<br/>de Bancs abierto?"}
    CB -- "si" --> ESP["No reclama eventos:<br/>quedan PENDING, no gastan reintentos"]
    ESP --> Q
    CB -- "no" --> LOTE["Reclama hasta 100 eventos<br/>FOR UPDATE SKIP LOCKED<br/>y los envia en UNA peticion"]
    LOTE --> BANCS["Bancs<br/>aplica solo si sequence es mayor<br/>que la ultima aplicada"]
    BANCS -- "200 OK" --> OK["Evento SENT<br/>+ fila en bancs_sync_log (SUCCESS)"]
    BANCS -- "503 / timeout" --> FALLO["Fila en bancs_sync_log (FAILED)<br/>reintento con backoff exponencial + jitter"]
    FALLO --> R{"¿5 reintentos?"}
    R -- "no" --> Q
    R -- "si" --> DEAD["Evento DEAD<br/>(cola de errores, revision manual)"]
    OK --> CONC["Conciliacion (admin, manual):<br/>GET /api/v1/admin/reconciliation<br/>MATCH / PENDING_SYNC / DISCREPANCY"]
```

---

## 4. Pipeline ETL

```mermaid
flowchart LR
    RAW["raw_transactions.csv<br/>4.415 filas con basura"] --> EX["EXTRACT<br/>lee y cuenta filas<br/>(detecta columnas de mas o de menos)"]
    EX --> CL["CLEAN<br/>fechas a ISO-8601 UTC, montos a Decimal,<br/>divisas ISO-4217, trim, imputacion, deduplicacion"]
    CL -->|"3.954 limpias"| TR["TRANSFORM<br/>categoria, agregados cliente-dia,<br/>marcas de comportamiento atipico"]
    CL -->|"461 irrecuperables"| Q["etl/data/quarantine/<br/>(con el motivo, nada se descarta en silencio)"]
    TR --> LO["LOAD"]
    LO --> PQ["Parquet<br/>(columnar, comprimido zstd)"]
    LO --> DBT[("Tablas analiticas<br/>analytics_transactions<br/>analytics_customer_daily")]
    LO --> REP["Reporte: leidos = limpios + cuarentena<br/>y duracion por etapa"]
```

---

## 5. Deadlock: orden invertido vs. orden fijo

### 5.1 Lo que pasa SIN orden fijo (modo inseguro)

A→B bloquea A y luego pide B. B→A bloquea B y luego pide A. Cada una espera lo que tiene la otra: **ciclo = deadlock**. PostgreSQL lo detecta tras 1 s (`deadlock_timeout`) y **aborta una de las dos**.

```mermaid
sequenceDiagram
    participant T1 as Transferencia 1 (A→B)
    participant A as Fila cuenta A
    participant B as Fila cuenta B
    participant T2 as Transferencia 2 (B→A)

    T1->>A: FOR UPDATE (obtiene el bloqueo)
    T2->>B: FOR UPDATE (obtiene el bloqueo)
    T1->>B: FOR UPDATE ... ESPERA (la tiene T2)
    T2->>A: FOR UPDATE ... ESPERA (la tiene T1)
    Note over T1,T2: CICLO: T1 espera a T2 y T2 espera a T1
    Note over T1,T2: 1 s despues PostgreSQL aborta una: ERROR 40P01 deadlock detected
```

### 5.2 Lo que pasa CON orden fijo (nuestra solución, ADR 0003)

Las dos transferencias piden primero la cuenta de **menor id** (aquí, A). La segunda simplemente espera a que la primera termine. Sin ciclo posible.

```mermaid
sequenceDiagram
    participant T1 as Transferencia 1 (A→B)
    participant A as Fila cuenta A (id menor)
    participant B as Fila cuenta B (id mayor)
    participant T2 as Transferencia 2 (B→A)

    T1->>A: FOR UPDATE ... ORDER BY id (obtiene A)
    T1->>B: (obtiene B, en la misma sentencia)
    T2->>A: FOR UPDATE ... ORDER BY id ... ESPERA (la tiene T1)
    T1->>T1: actualiza saldos y hace COMMIT (libera A y B)
    T2->>A: obtiene A
    T2->>B: obtiene B
    T2->>T2: actualiza saldos y hace COMMIT
    Note over T1,T2: Nadie espera en circulo. Resultado medido: 0 deadlocks
```

**Resultado medido** (`evidence/incident/deadlock_demo.txt`, 30 transferencias cruzadas a la vez):

| | Orden invertido | Orden por id (la API real) |
|---|---|---|
| Completadas | 1 | 30 |
| Abortadas por deadlock | **29** | **0** |
