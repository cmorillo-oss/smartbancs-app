# ADR 0006 — La IA es asíncrona y vive fuera del camino crítico

- **Estado:** Aceptado (con una enmienda basada en evidencia, ver "Enmienda") · **Fase:** 5

## Contexto
La IA de recomendaciones tarda 300-800 ms (medido: ~610-635 ms de media) y puede caerse. El reto exige
transferencias en < 2 s y que la IA **no bloquee ni retrase** el flujo transaccional. Llamarla de forma
síncrona sumaría ~0.6 s a **cada** transferencia y, si la IA se cae, cada transferencia esperaría un timeout.

## Decisión
1. **La respuesta HTTP nunca espera a la IA.** La recomendación se calcula después y se guarda en
   `ai_recommendations`; el cliente la consulta con `GET /api/v1/customers/{id}/recommendations`
   (lectura de la BD local; si aún no hay ninguna, devuelve una recomendación genérica en memoria: el *fallback*).
2. **El evento `ai.transaction_created` se escribe en el outbox dentro de la transacción** (ADR 0004). Es la fuente
   de verdad de "esta transferencia debe notificarse a la IA": sobrevive a caídas de cualquier proceso.
3. **Entrega "reclamar → procesar → confirmar"** (`outbox_repository.py`): el evento pasa a `PROCESSING`
   (con *lease*), se llama a la IA **sin ninguna conexión de BD abierta**, y se marca `SENT` / reintento con
   backoff / `DEAD` tras 5 fallos. Un *lease* caducado devuelve el evento a `PENDING` (recuperación tras un crash).
4. **Protecciones en el cliente** (`ai_client.py`): timeout duro de 1 s (`httpx` + `asyncio.timeout`), circuit
   breaker (ADR 0007), mamparo de 50 llamadas simultáneas, y ninguna excepción se propaga.

## Enmienda: quién entrega (medido, no supuesto)
El brief propone `BackgroundTasks` tras el commit. Se implementó (`AI_NOTIFY_IN_PROCESS=true`) y se **midió**:
- La respuesta no espera a la IA (comprobado: 23 ms de respuesta frente a 564 ms que tarda la tarea completa) y
  una petición posterior en la misma conexión *keep-alive* tampoco espera (33 ms, sonda dedicada).
- Pero la tarea de fondo **comparte proceso** con la API: event loop, pool de BD, hilos de resolución DNS y Postgres.
  Con la IA apagada, resolver `ai-service` tarda **3.3-3.5 s en fallar** (`evidence/ai-resilience/dns_check.txt`).
  En tres ejecuciones intermedias del arnés (no conservadas como evidencia) ese modo mostró rondas con
  transferencias de ~13 s con la IA apagada, coherentes con esas búsquedas DNS ocupando los hilos de resolución
  que la API también necesita para abrir conexiones a Postgres. **La última medición (conservada) no lo
  reprodujo**, así que la causa raíz es una hipótesis fuerte, no una demostración.
- La medición conservada sí muestra que, con la IA encendida, el modo BackgroundTasks fue el más lento en ráfaga
  (p95 4940 ms frente a 3350 ms con la IA apagada; una sola pareja de mediciones).
- Por eso el **valor por defecto es `AI_NOTIFY_IN_PROCESS=false`**: solo el **worker** (otro proceso, otro pool,
  otro breaker) habla con la IA y la API no tiene ninguna dependencia de ella. La entrega se retrasa como
  máximo el intervalo de sondeo (2 s), irrelevante para una recomendación. El modo del brief sigue disponible.

## Consecuencias
- (+) La transferencia no depende de la IA en ningún punto: con la IA encendida, apagada o colgada, 100 % de
  las transferencias se completan (~5400 transferencias medidas en la batería final, 0 errores).
- (+) Tras una caída, el backlog se recupera solo: el worker vació los eventos acumulados (cientos a miles, según
  la duración de la caída) sin intervención; en una prueba previa, ~1200 eventos en ~65 s.
- (−) Latencia de la recomendación = latencia de la IA + hasta 2 s de sondeo.
- (−) El trabajo de la IA sigue compartiendo Postgres con las transferencias (mismo servidor de BD).
  La solución de fondo es una réplica de lectura para el historial y limitar el ritmo del worker.
