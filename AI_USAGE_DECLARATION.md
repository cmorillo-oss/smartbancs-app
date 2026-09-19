# Declaración de uso de Inteligencia Artificial

**Proyecto:** SmartBancs App (reto técnico)
**Autora:** Cielo Morillo
**Fecha de la declaración:** 2026-09-19

> **Cómo leer este documento.** Lo escribió Claude Code a partir del historial del repositorio y de las instrucciones que la autora dio para su elaboración.
> Todo lo marcado con **[COMPLETAR]** lo debe rellenar la autora con lo que **solo ella sabe** (qué revisó, qué cambió, cuánto tiempo, qué hizo a mano).
> **Nada marcado como "hecho por la autora" debe quedar sin que ella lo confirme.** Una declaración de IA con afirmaciones que no son ciertas es peor que no tenerla.

---

## 1. Resumen honesto

Este proyecto se hizo **con mucho apoyo de IA**, y aquí se dice con claridad:

- La **implementación del código** (servicios, SQL, pruebas, scripts, documentación) la escribió **Claude Code**, siguiendo un brief por fases (`CLAUDE_CODE_BRIEF.md`).
- El **análisis del reto, la planificación y el aprendizaje** se hicieron conversando con **Claude (chat)**.
- La **autora** decidió el alcance, revisó cada fase, ejecutó y verificó las pruebas y subió los cambios al repositorio.

La autora es responsable de todo lo que se entrega y debe poder defenderlo oralmente. Por eso el código lleva comentarios en español que explican el **porqué** de cada decisión.

---

## 2. Herramientas usadas

| Herramienta | Modelo / versión | Para qué se usó |
|---|---|---|
| **Claude** (chat, claude.ai) | **[COMPLETAR: modelo y fechas aproximadas]** | Análisis del enunciado del reto, planificación, diseño del brief por fases, y **aprendizaje** de los conceptos (deadlocks, outbox, circuit breaker, idempotencia, PSI/KS, etc.) |
| **Claude Code** (CLI) | Claude Sonnet 5 en la sesión de la Fase 8; **[COMPLETAR: modelo(s) usado(s) en las Fases 1-7]** | Implementación guiada por fases a partir de `CLAUDE_CODE_BRIEF.md`; depuración; redacción de la documentación |
| Otras herramientas de IA | **[COMPLETAR: p. ej. GitHub Copilot u otras; escribir "ninguna" si no se usó ninguna]** | **[COMPLETAR]** |

---

## 3. Qué hizo cada herramienta, por etapa

### 3.1 Claude (chat): análisis, planificación y aprendizaje
- Análisis del enunciado del reto y de sus tres restricciones (concurrencia, Bancs legado, latencia).
- Planificación: se decidió el **orden de las fases** y las **reglas de oro** (nunca llamar a servicios externos dentro de una transacción, bloquear cuentas en orden determinista, etc.).
- Elaboración del brief `CLAUDE_CODE_BRIEF.md`, que sirvió como especificación para Claude Code.
- Explicaciones de conceptos para que la autora pudiera **defenderlos**.

**[COMPLETAR: cuántas conversaciones, qué preguntas de aprendizaje hizo la autora, qué conceptos dice que entendió a fondo y cuáles todavía son débiles. Ser honesta aquí ayuda: el jurado valora saber qué domina cada quien.]**

### 3.2 Claude Code: implementación por fases
El trabajo se hizo **una fase por vez**, con detención al final de cada una para que la autora verificara (regla del brief). Cada fase quedó en un commit propio:

| Fase | Contenido | Commit |
|---|---|---|
| 1 | Fundaciones, esquema de BD, docker-compose y API base | `519aa29` |
| 2 | Observabilidad (logs JSON, métricas y trazas) | `2ca5d3f` |
| 3 | Transferencia con control de concurrencia | `c68ca0f` |
| 4 | Pruebas de concurrencia y demo de la condición de carrera | `b329672` |
| 5 | Servicio de IA, circuit breaker, worker del outbox, prueba de resiliencia | `9e43e0c` |
| 6 | Bancs mock, sincronización por lotes, conciliación y ETL | `9773503` |
| 7 | Incidente simulado, diagnóstico, alertas y prueba de carga | `916a8ff` |
| 8 | Documentación y entrega (este documento incluido) | commit de la Fase 8 |

Componentes implementados con Claude Code: los tres servicios (`transaction-api`, `ai-service`, `bancs-mock`), el worker, el esquema y los índices SQL, el ETL, las pruebas (`tests/`), los scripts, las alertas de Prometheus, los 9 ADR y los documentos de la Fase 8.

### 3.3 Documentación de la Fase 8
`README.md`, `docs/DOCUMENTO_TECNICO.md`, `docs/POSTMORTEM.md`, `docs/diagrams/DIAGRAMAS.md` y **este archivo** los redactó Claude Code a partir del código, de los archivos de `evidence/` y del historial de git. Las cifras se copiaron de la evidencia guardada; las **proyecciones** (10.000 TPS) y los **diseños no implementados** (ciclo de vida del modelo de IA) están rotulados como tales dentro de los documentos.

---

## 4. Qué hizo la autora (a confirmar)

Esta es la parte más importante de la declaración y **solo ella puede completarla**. Lo siguiente es lo que se le indicó a Claude Code que declarara; la autora debe confirmar cada punto:

| Afirmación | ¿Confirmada por la autora? |
|---|---|
| Decidió el alcance del proyecto y qué fases se hacían | **[COMPLETAR: sí / no / matiz]** |
| Revisó el resultado de **cada fase** antes de aprobar la siguiente | **[COMPLETAR]** |
| **Ejecutó** las pruebas y demos por su cuenta (`make test-concurrency`, `make demo-race`, `make deadlock-demo`, `make load`, etc.) y verificó los resultados | **[COMPLETAR]** |
| **Subió los cambios** al repositorio (`git push`); Claude Code tiene prohibido hacerlo por regla del proyecto (`CLAUDE.md`) | **[COMPLETAR]** |
| Puede explicar oralmente el algoritmo de la transferencia, el orden de bloqueo y el patrón outbox | **[COMPLETAR]** |

**[COMPLETAR: en sus propias palabras, qué partes del código leyó línea por línea, cuáles modificó ella misma, y qué escribió o decidió sin ayuda de la IA (p. ej. elección del alcance, prioridades, qué demos mostrar en el video). Ejemplo del nivel de detalle esperado: "Modifiqué X porque Y".]**

---

## 5. Qué se revisó y validó manualmente

Lo que **está verificado en el repositorio** (y cualquiera puede repetirlo):

| Qué | Cómo se validó | Evidencia |
|---|---|---|
| No hay race conditions | Pruebas de concurrencia: 100 transferencias simultáneas de 50,00 sobre 1.000,00 → exactamente 20 triunfan, saldo 0,00; suma cero; idempotencia; ledger | `evidence/test-data/test_concurrency_output.txt` (4/4) |
| El código inseguro sí falla | Demo comparativo contra una versión sin `FOR UPDATE` | `evidence/test-data/demo_race_condition_output.txt` |
| El orden de bloqueo evita deadlocks | 29 deadlocks vs. 0, contados en PostgreSQL | `evidence/incident/deadlock_demo.txt` |
| La IA no afecta a las transferencias | Medición con IA encendida, apagada y colgada | `evidence/ai-resilience/comparison.txt` |
| Bancs no se satura con lotes | Medición de degradación y demo de caída | `evidence/bancs/` |
| El pool agotado se detecta y diagnostica | Reproducción del incidente | `evidence/incident/exhaust_pool.txt` |
| Capacidad real | Prueba de carga con Locust | `evidence/load-test-results/` |

**Errores de la IA encontrados al probar** (evidencia de que se verificó y no se aceptó todo a ciegas):
1. La primera versión del endpoint `blocking-tree` tenía coste exponencial y **consumió 2,5 GB de RAM y congeló la API** durante el incidente; se corrigió y se añadió un test de regresión (ver `docs/POSTMORTEM.md`, Incidente 2).
2. La alerta `DatabasePoolExhausted` **no disparaba** en su primera versión; se descubrió al ejecutar el incidente real (ADR 0009).
3. Los `.sql` del directorio de inicialización de Postgres **no se ejecutaban** al montar carpetas enteras; se descubrió al verificar (comentario en `docker-compose.yml`).
4. En el modo `BackgroundTasks` de la IA se midieron efectos de latencia por DNS; se cambió el diseño por defecto (ADR 0006).
5. Un script de carga se quedaba girando 20 minutos por `set -e` + `pipefail` con Locust (comentario en `scripts/load_test.sh`).

**Limitaciones de esta validación (dicho con honestidad):**
- Las salidas de las pruebas unitarias y de integración **no están guardadas** en `evidence/` (solo la de concurrencia); deben regenerarse con `make test`.
- La corrida de carga guardada es anterior al cambio 500 → 503 en lecturas; conviene repetirla.
- **[COMPLETAR: si la autora encontró otros errores, o detectó afirmaciones de la IA que no eran ciertas y las corrigió, listarlos aquí.]**

---

## 6. Qué NO se hizo con IA / qué no se delegó

**[COMPLETAR por la autora.]** Ejemplos de lo que suele ir aquí: decisiones de prioridad y alcance, la grabación del video, la defensa oral, la verificación final en su propia máquina, la subida al repositorio.

---

## 7. Riesgos y cómo se mitigaron

| Riesgo de usar IA | Mitigación aplicada |
|---|---|
| Código correcto "en apariencia" pero con fallos de concurrencia | Pruebas empíricas de concurrencia y demo contra la versión insegura; deadlocks contados en PostgreSQL, no en el script |
| Que la autora no entienda lo entregado | Comentarios en español que explican el **porqué**; ADR por cada decisión; documento técnico con la defensa; una fase por vez con revisión |
| Cifras inventadas o infladas | Toda cifra de la documentación sale de un archivo de `evidence/`; las proyecciones están rotuladas; hay una sección de limitaciones |
| Que la IA haga cambios irreversibles | Regla del proyecto (`CLAUDE.md`): **Claude Code nunca hace `git push`** y sus commits **no llevan atribución ni `Co-Authored-By`** (el commit lo hace la autora o se hace sin trailers) |
| Alucinaciones en la documentación | Revisión de la autora **[COMPLETAR: confirmar que revisó los documentos de la Fase 8 contra el código]** |

> **Nota de transparencia sobre los commits.** Por instrucción de la autora (`CLAUDE.md`), los commits del proyecto **no incluyen** líneas `Co-Authored-By` ni atribución a Claude. Esa decisión es de estilo del repositorio y **no oculta** el uso de IA: este archivo existe justamente para declararlo.

---

## 8. Declaración final

Declaro que el uso de herramientas de IA en este proyecto es el descrito arriba, que revisé el resultado y que puedo explicar y defender el diseño y el código entregados.

**Firma:** **[COMPLETAR: nombre y firma de la autora]**
**Fecha:** **[COMPLETAR]**
