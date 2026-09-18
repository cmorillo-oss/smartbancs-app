-- =============================================================================
-- SmartBancs - Esquema base (Fase 1)
-- Se ejecuta automáticamente al primer arranque de Postgres porque el compose
-- monta este directorio en /docker-entrypoint-initdb.d/ (orden alfabético:
-- 01_schema -> 02_indexes -> 03_seed).
-- =============================================================================

-- Cuentas: el saldo local es "saldo disponible" = saldo Bancs sincronizado - reservas locales
-- POR QUÉ un saldo local: Bancs (core legado) se degrada con consultas directas, así que
-- el camino crítico opera SOLO contra esta tabla y sincroniza con Bancs de forma asíncrona.
CREATE TABLE accounts (
    id              BIGSERIAL PRIMARY KEY,
    account_number  VARCHAR(20)  NOT NULL UNIQUE,
    customer_id     VARCHAR(36)  NOT NULL,
    balance         NUMERIC(18,2) NOT NULL DEFAULT 0,   -- NUMERIC, nunca FLOAT: el dinero no admite error de redondeo binario
    currency        CHAR(3)      NOT NULL DEFAULT 'USD',
    status          VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    version         INTEGER      NOT NULL DEFAULT 0,    -- contador de cambios: auditoría y detección de lecturas obsoletas
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Última línea de defensa: aunque falle la lógica, la BD no permite saldo negativo
    CONSTRAINT chk_balance_non_negative CHECK (balance >= 0),
    CONSTRAINT chk_status CHECK (status IN ('ACTIVE','BLOCKED','CLOSED'))
);

-- Transacciones: una fila por transferencia.
-- POR QUÉ UUID como PK: el id se genera en la aplicación, así se puede devolver al cliente
-- y usar como aggregate_id del outbox sin un round-trip extra a la BD.
CREATE TABLE transactions (
    id                UUID PRIMARY KEY,
    idempotency_key   VARCHAR(64) NOT NULL UNIQUE,  -- garantiza no-duplicación: el UNIQUE es el árbitro final incluso con 50 peticiones simultáneas
    source_account_id BIGINT      NOT NULL REFERENCES accounts(id),
    dest_account_id   BIGINT      NOT NULL REFERENCES accounts(id),
    amount            NUMERIC(18,2) NOT NULL,
    currency          CHAR(3)     NOT NULL,
    status            VARCHAR(20) NOT NULL,
    trace_id          VARCHAR(64),                  -- correlaciona la fila con logs y trazas (regla de oro 4)
    error_code        VARCHAR(50),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    CONSTRAINT chk_amount_positive CHECK (amount > 0),
    CONSTRAINT chk_different_accounts CHECK (source_account_id <> dest_account_id),
    CONSTRAINT chk_tx_status CHECK (status IN ('PENDING','COMPLETED','FAILED','REVERSED'))
);

-- Partida doble: auditabilidad total, requisito de facto en banca
-- Invariante verificable: SUM(DEBIT) == SUM(CREDIT). Se prueba en la Fase 4.
CREATE TABLE ledger_entries (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id UUID        NOT NULL REFERENCES transactions(id),
    account_id     BIGINT      NOT NULL REFERENCES accounts(id),
    entry_type     VARCHAR(10) NOT NULL,
    amount         NUMERIC(18,2) NOT NULL,
    balance_after  NUMERIC(18,2) NOT NULL,          -- foto del saldo tras el asiento: reconstruye el historial sin recalcular
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_entry_type CHECK (entry_type IN ('DEBIT','CREDIT'))
);

-- Patrón Outbox: desacopla la transacción de las integraciones (Bancs, IA)
-- El evento se inserta en la MISMA transacción que el cambio de saldo: o existen ambos
-- o ninguno. Da entrega at-least-once sin 2PC contra el legado.
CREATE TABLE outbox_events (
    id             BIGSERIAL PRIMARY KEY,
    aggregate_id   UUID        NOT NULL,
    event_type     VARCHAR(50) NOT NULL,
    payload        JSONB       NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    retry_count    INTEGER     NOT NULL DEFAULT 0,
    next_retry_at  TIMESTAMPTZ,                     -- backoff exponencial: el worker ignora el evento hasta esta hora
    trace_id       VARCHAR(64),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at   TIMESTAMPTZ,
    CONSTRAINT chk_outbox_status CHECK (status IN ('PENDING','PROCESSING','SENT','FAILED','DEAD'))  -- DEAD = dead letter tras agotar reintentos
);

-- Bitácora de lotes enviados a Bancs: base de la reconciliación y auditoría de la integración
CREATE TABLE bancs_sync_log (
    id             BIGSERIAL PRIMARY KEY,
    batch_id       UUID        NOT NULL,
    events_count   INTEGER     NOT NULL,
    status         VARCHAR(20) NOT NULL,
    latency_ms     INTEGER,
    error_message  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Recomendaciones de IA. La FK a transactions es NULLABLE a propósito:
-- una recomendación puede ser por cliente (patrón de gasto) y no por una transacción concreta.
CREATE TABLE ai_recommendations (
    id             BIGSERIAL PRIMARY KEY,
    customer_id    VARCHAR(36) NOT NULL,
    transaction_id UUID REFERENCES transactions(id),
    recommendation JSONB       NOT NULL,
    model_version  VARCHAR(20) NOT NULL,             -- versionado del modelo: necesario para rollback y análisis de drift
    latency_ms     INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
