# ADR 0002 — Bloqueo pesimista (`SELECT ... FOR UPDATE`) frente a versionado optimista

- **Estado:** Aceptado · **Fase:** 3

## Contexto
Dos transferencias simultáneas sobre la misma cuenta no pueden leer el mismo saldo y escribir
cada una su resultado (pérdida de actualización / sobregiro). Hay dos familias de solución:
- **Optimista:** leer con `version`, escribir con `UPDATE ... WHERE version = :v`; si afectó 0 filas, alguien
  se adelantó y se reintenta.
- **Pesimista:** bloquear las filas (`FOR UPDATE`) antes de validar y modificar.

## Decisión
Bloqueo pesimista con `FOR UPDATE` en READ COMMITTED, con `lock_timeout` de 3 s.

## Razones
- En banca el conflicto **no es raro**: cuentas de nóminas, comercios y la cuenta de una empresa
  reciben muchas operaciones a la vez. Bajo contención alta el enfoque optimista produce
  **tormentas de reintentos**: cada conflicto repite la lectura, la validación y las escrituras, y el
  trabajo desperdiciado crece justo cuando el sistema está más cargado.
- **Esperar en cola es más barato que reintentar.** Con `FOR UPDATE` la segunda transferencia espera
  milisegundos y continúa una sola vez; no repite trabajo.
- Tras obtener el bloqueo, las reglas (saldo suficiente, cuenta activa) se validan sobre datos que
  **ya no pueden cambiar** hasta el commit: se elimina el "check-then-act".
- La columna `version` se mantiene igualmente (auditoría), pero no es el mecanismo de control.

## Consecuencias
- (+) Correctitud simple de razonar; latencia predecible bajo contención.
- (−) Riesgo de **deadlock** si el orden de bloqueo no es uniforme → resuelto en ADR 0003.
- (−) Una transacción lenta retiene bloqueos: por eso ninguna llamada externa ocurre dentro de la
  transacción y existe `lock_timeout` (falla rápido en lugar de colgar el pool).
- Descartado `NOWAIT`: convertiría la contención normal en errores para el cliente.
