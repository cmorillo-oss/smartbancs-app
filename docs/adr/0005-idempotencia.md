# ADR 0005 — Idempotencia con `Idempotency-Key`

- **Estado:** Aceptado · **Fase:** 3

## Contexto
Ante un timeout de red el cliente no sabe si su transferencia se aplicó. Si reintenta sin garantías,
podría mover el dinero dos veces. La regla de oro 5 exige que toda escritura de dinero sea idempotente.

## Decisión
- Cabecera **obligatoria** `Idempotency-Key` (1-64 caracteres: letras, dígitos, `-`, `_`).
- Se guarda en `transactions.idempotency_key` con restricción **UNIQUE**.
- Flujo:
  1. Consulta previa sin bloqueos: si existe → **HTTP 200** + `Idempotent-Replay: true` con el resultado almacenado.
  2. Tras bloquear las cuentas se **vuelve a comprobar**: si N peticiones con la misma clave llegan a la
     vez, los bloqueos las serializan y solo la primera mueve dinero.
  3. El **UNIQUE** es el árbitro final: si aun así hay carrera, el INSERT falla, la transacción hace
     rollback y se devuelve el resultado de la ganadora.
- Misma clave con **cuerpo distinto** → **HTTP 409** `IDEMPOTENCY_KEY_CONFLICT` (devolver el resultado
  viejo haría creer al cliente que su nueva orden se ejecutó).
- Los montos se normalizan a 2 decimales para que la comparación de cuerpos no falle por formato (50.1 vs 50.10).

## Decisión relacionada: los fallos de negocio no se persisten
Un `INSUFFICIENT_FUNDS` hace rollback y no deja fila `FAILED`; solo se cuenta en métricas y logs. Así,
tras recargar la cuenta el cliente puede reintentar con la misma clave. (El estado `FAILED` del esquema
queda disponible para casos futuros, p. ej. fallos asíncronos.)

## Consecuencias
- (+) Reintentos seguros; base de la prueba de "50 peticiones simultáneas, 1 transacción" (Fase 4).
- (−) Las claves se conservan indefinidamente en `transactions`; en producción habría una política de retención.
