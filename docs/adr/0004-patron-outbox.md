# ADR 0004 — Patrón Outbox (sin 2PC contra Bancs)

- **Estado:** Aceptado · **Fase:** 3

## Contexto
Tras mover el dinero hay que avisar a Bancs (core legado) y a la IA. Escribir en la BD y llamar a un
sistema externo son dos operaciones que no pueden ser atómicas entre sí. Opciones:
1. Llamar a Bancs dentro de la transacción → viola la regla de oro 1 (retiene bloqueos y una conexión
   mientras un sistema lento responde) y satura al legado.
2. Commit y después llamar → si el proceso muere entre ambos, el cambio existe y el aviso se pierde.
3. Transacción distribuida (2PC) → Bancs es un legado que probablemente no lo soporta, y 2PC bloquea
   recursos y reduce disponibilidad.
4. **Outbox.**

## Decisión
Insertar en `outbox_events` (`bancs.balance_updated` y `ai.transaction_created`) **en la misma
transacción** que el cambio de saldo. Un worker (Fase 5) los lee y los envía con reintentos.

## Razones
- **Atomicidad:** o existen el saldo, el asiento y el evento, o ninguno.
- **At-least-once:** si el proceso muere tras el commit, el evento sigue en la tabla y se reenvía.
  Los consumidores deben ser idempotentes (el `aggregate_id` = id de transacción sirve de clave).
- **Aísla al legado:** el worker agrupa en lotes y controla el ritmo; el pico de 10.000 TPS no llega a Bancs.
- **La transferencia no depende de nadie:** con Bancs o la IA caídos, sigue completando.

## Consecuencias
- (+) Sin 2PC, sin llamadas externas en la transacción, resiliencia ante caídas.
- (−) Consistencia **eventual** con Bancs (el saldo local se adelanta); se controla con la
  reconciliación de la Fase 6.
- (−) La tabla crece: índice sobre `(status, next_retry_at)` y limpieza de eventos SENT.
