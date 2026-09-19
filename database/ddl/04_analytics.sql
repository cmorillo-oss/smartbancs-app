-- =============================================================================
-- Tablas ANALITICAS (Fase 6): las llena el pipeline ETL (etl/transform.py).
-- Son idempotentes (IF NOT EXISTS) porque el ETL tambien las ejecuta al arrancar:
-- asi funciona incluso sobre un volumen creado antes de esta fase.
-- Estan SEPARADAS de las tablas transaccionales: el analisis y la IA no compiten con las
-- transferencias por bloqueos ni por indices.
-- =============================================================================

-- Una fila por transaccion limpia y enriquecida (misma informacion que el Parquet).
CREATE TABLE IF NOT EXISTS analytics_transactions (
    transaction_id         VARCHAR(64)   PRIMARY KEY,
    customer_id            VARCHAR(36)   NOT NULL,
    account_number         VARCHAR(20)   NOT NULL,
    occurred_at            TIMESTAMPTZ   NOT NULL,
    amount                 NUMERIC(18,2) NOT NULL,
    currency               CHAR(3)       NOT NULL,
    merchant               VARCHAR(100)  NOT NULL,
    description            VARCHAR(255),
    category               VARCHAR(40)   NOT NULL,
    flag_atypical_amount   BOOLEAN       NOT NULL,
    flag_high_frequency_day BOOLEAN      NOT NULL,
    etl_run_id             VARCHAR(36)   NOT NULL,
    loaded_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Agregado por cliente y dia (total, promedio, conteo, desviacion): lo que consume la IA.
CREATE TABLE IF NOT EXISTS analytics_customer_daily (
    customer_id  VARCHAR(36)   NOT NULL,
    day          DATE          NOT NULL,
    tx_count     INTEGER       NOT NULL,
    total_amount NUMERIC(18,2) NOT NULL,
    avg_amount   NUMERIC(18,2) NOT NULL,
    std_amount   NUMERIC(18,2) NOT NULL,
    max_amount   NUMERIC(18,2) NOT NULL,
    etl_run_id   VARCHAR(36)   NOT NULL,
    PRIMARY KEY (customer_id, day)
);

-- Historial de un cliente en orden cronologico inverso (consulta tipica del analisis por cliente).
CREATE INDEX IF NOT EXISTS idx_analytics_tx_customer ON analytics_transactions (customer_id, occurred_at DESC);
-- Reportes por categoria.
CREATE INDEX IF NOT EXISTS idx_analytics_tx_category ON analytics_transactions (category);
