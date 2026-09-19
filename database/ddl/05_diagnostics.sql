-- Extension para el endpoint /api/v1/admin/diagnostics/slow-queries (Fase 7).
-- Requiere shared_preload_libraries=pg_stat_statements (configurado en el comando de postgres en docker-compose).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
