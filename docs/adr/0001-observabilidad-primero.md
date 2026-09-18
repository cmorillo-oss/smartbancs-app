# ADR 0001 — Observabilidad primero: métricas RED y `trace_id` propagado

- **Estado:** Aceptado
- **Fase:** 2

## Contexto
El reto exige detectar y diagnosticar degradación bajo picos de hasta 10.000 TPS, con un core
legado frágil y transferencias que deben completar en < 2 s. Sin instrumentación no se puede
saber *qué* se degrada ni *dónde*; y añadirla tras escribir la lógica sale mucho más caro y
queda superficial.

## Decisión
1. **Instrumentar antes de escribir el endpoint de transferencias** (Fase 2 antes que la 3).
2. **Métricas con el método RED** (Rate, Errors, Duration) para el servicio:
   `transactions_total`, `transaction_errors_total`, `transaction_duration_seconds`.
   Complementadas con señales del recurso crítico (método USE): pool de conexiones, bloqueos,
   deadlocks; y de las dependencias: IA, Bancs, outbox.
3. **`trace_id` propagado con `contextvars`** y presente en todo log, en la tabla
   `transactions`/`outbox_events` y como atributo del span de OpenTelemetry.
4. **Logs JSON por stdout** (structlog), incluyendo los de uvicorn y SQLAlchemy vía `logging` estándar.

## Por qué RED
Responde en segundos las tres preguntas de un incidente: ¿cuánto tráfico entra? (Rate),
¿cuánto falla? (Errors), ¿cuánto tarda? (Duration). Además mapea directo a los SLO del reto:
los buckets del histograma tienen un límite exacto en 2 s para calcular el p95 contra el
requisito de latencia.

## Por qué `trace_id` propagado
Una transferencia toca API → PostgreSQL → outbox → worker → IA/Bancs. Con un mismo `trace_id`
en cada log y fila se reconstruye su recorrido completo con un solo filtro. `contextvars` lo
aísla por tarea asyncio y evita pasarlo como parámetro por todas las funciones. El cliente puede
aportarlo (`X-Trace-Id`) o se genera (UUID4), y se le devuelve en la respuesta para soporte.

## Decisiones menores relevantes
- **Middleware ASGI puro** (no `BaseHTTPMiddleware`): menor sobrecarga y no pierde el contexto
  en `BackgroundTasks`.
- **Labels de baja cardinalidad**: nunca ids de cuenta/usuario; `operation` acotada a un conjunto fijo.
- **`/health` (liveness) no toca la BD; `/ready` sí**: evita reinicios en bucle cuando cae Postgres.
- **Trazas exportadas por lotes y solo si hay endpoint configurado**: la observabilidad nunca
  debe añadir latencia ni ser un punto único de fallo.
- El `trace_id` propio y el trace id interno de OpenTelemetry son distintos; se enlazan con el
  atributo `smartbancs.trace_id` del span.

## Consecuencias
- (+) Diagnóstico rápido y evidencia medible para las Fases 4, 5 y 7.
- (−) Pequeño coste de CPU por request (mitigado: middleware ligero, exportación por lotes).
- (−) El estado `waiting` del pool no lo expone SQLAlchemy; se resolverá en la Fase 7.
