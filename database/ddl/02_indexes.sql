-- =============================================================================
-- Índices. Cada uno responde a UNA consulta concreta; no se crean "por si acaso"
-- porque cada índice encarece las escrituras y aquí el camino crítico es escribir.
-- (Ya existen índices implícitos por PK y por los UNIQUE: accounts.account_number y
--  transactions.idempotency_key.)
-- =============================================================================

-- Acelera la consulta del worker del outbox:
--   SELECT ... FROM outbox_events WHERE status='PENDING' AND (next_retry_at IS NULL OR next_retry_at <= NOW())
--   ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED
CREATE INDEX idx_outbox_status_retry ON outbox_events (status, next_retry_at);

-- Acelera el historial paginado de una cuenta origen:
--   WHERE source_account_id = :id ORDER BY created_at DESC LIMIT n
-- El orden DESC en el índice evita un sort en memoria.
CREATE INDEX idx_tx_source_created ON transactions (source_account_id, created_at DESC);

-- Conteos y búsquedas por estado (p.ej. transacciones PENDING/FAILED en diagnóstico y reconciliación).
CREATE INDEX idx_tx_status ON transactions (status);

-- Estado de cuenta: asientos de una cuenta ordenados por fecha descendente.
CREATE INDEX idx_ledger_account_created ON ledger_entries (account_id, created_at DESC);

-- "Recomendaciones de este cliente" (lectura desde la app y el ETL).
CREATE INDEX idx_ai_reco_customer ON ai_recommendations (customer_id);
