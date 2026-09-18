-- =============================================================================
-- Datos de prueba: 20 cuentas con saldos CONOCIDOS para que las pruebas puedan
-- calcular el resultado esperado (suma total, sobregiros, etc.) sin adivinar.
-- =============================================================================

-- Cuenta dedicada a la prueba de sobregiro concurrente de la Fase 4:
-- 1000.00 exactos / transferencias de 50.00 => deben triunfar exactamente 20.
INSERT INTO accounts (account_number, customer_id, balance, currency) VALUES
    ('ACC-TEST-CONCURRENCY', 'CUST-CONC-0001', 1000.00, 'USD');

-- Cuenta receptora de esa prueba: así el destino no interfiere con otras cuentas.
INSERT INTO accounts (account_number, customer_id, balance, currency) VALUES
    ('ACC-TEST-SINK', 'CUST-CONC-0002', 0.00, 'USD');

-- 18 cuentas generales con saldos escalonados (5000, 10000, ... 90000):
-- suma total conocida = 5000 * (1+2+...+18) = 855000.00 (base de la prueba de suma cero).
INSERT INTO accounts (account_number, customer_id, balance, currency)
SELECT 'ACC-' || LPAD(g::text, 6, '0'),
       'CUST-' || LPAD(g::text, 6, '0'),
       g * 5000.00,
       'USD'
FROM generate_series(1, 18) AS g;
