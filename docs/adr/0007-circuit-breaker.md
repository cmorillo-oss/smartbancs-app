# ADR 0007 — Circuit breaker propio para la IA

- **Estado:** Aceptado · **Fase:** 5

## Contexto
Si la IA falla o se cuelga, seguir llamándola desperdicia una tarea y una conexión por cada evento, esperando un
timeout que ya sabemos que llegará, y no deja recuperarse a la IA (recibe tráfico mientras intenta levantarse).

## Decisión
Un circuit breaker de tres estados (`services/circuit_breaker.py`, sin librerías externas):

```
CLOSED --(5 fallos CONSECUTIVOS)--> OPEN --(30 s)--> HALF_OPEN --(prueba OK)--> CLOSED
                                     ^                    |
                                     +----(prueba falla)--+
```
- **Consecutivos:** un éxito reinicia la cuenta; un fallo aislado no abre el circuito.
- **HALF_OPEN deja pasar UNA sola llamada de prueba.** Si dejara pasar todas, una IA que apenas se recupera
  recibiría de golpe todo el tráfico acumulado.
- **Con el circuito abierto no se llama**: se cuenta (`smartbancs_ai_calls_total{status="circuit_open"}`) y se sigue.
  Los eventos NO consumen reintentos (vuelven a `PENDING` intactos), así una caída larga no los lleva a `DEAD`.
- Estado expuesto en `smartbancs_ai_circuit_breaker_state` (0 CLOSED, 1 OPEN, 2 HALF_OPEN).
- Cada proceso (API, worker) tiene **su propio** breaker: no hay estado compartido que pueda fallar.
- Reloj inyectable: las pruebas (`tests/unit/test_circuit_breaker.py`, 8 casos) avanzan el tiempo sin dormir 30 s.
- Sin locks: todo ocurre en un único event loop y ninguna operación hace `await`, por lo que es atómica entre awaits.

## Alternativas descartadas
- Librería (`pybreaker`, etc.): añade una dependencia por ~80 líneas, y aquí importa poder explicar cada transición.
- Solo reintentos con backoff: reintenta a ciegas y no protege a la IA ni a nuestros recursos durante la caída.

## Consecuencias
- (+) Falla rápido y se recupera solo; observable en Prometheus.
- (−) Umbrales fijos (5 / 30 s); en producción convendría una ventana deslizante con porcentaje de errores.
- (−) Con varias réplicas de la API cada una abre su circuito por separado (aceptable: decisión local y barata).
