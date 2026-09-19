.PHONY: deadlock-demo exhaust-pool load diagnostics alerts-test etl bancs-sync-log bancs-degradation test-integration bancs-resilience up down logs test clean seed test-concurrency demo-race test-unit ai-down ai-up ai-resilience

# Levanta todo en segundo plano y reconstruye la imagen si cambió el código.
up:
	docker compose up -d --build

# Detiene los contenedores CONSERVANDO los datos (volumen pgdata).
down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# Todos los tests (por ahora: la suite de concurrencia).
test: test-unit test-concurrency test-integration

# Prueba de race conditions (Fase 4). Levanta Postgres + API si hace falta, corre pytest en un
# contenedor y guarda la salida como evidencia. `-T` evita pedir TTY (funciona también en CI).
test-concurrency:
	docker compose up -d --build postgres transaction-api
	docker compose --profile test run --rm -T tests pytest concurrency -v 2>&1 | tee evidence/test-data/test_concurrency_output.txt

# Comparativo INSEGURO vs SEGURO (va en el video). Guarda la salida lado a lado.
demo-race:
	docker compose up -d --build postgres transaction-api
	docker compose --profile test run --rm -T tests python concurrency/demo_race_condition.py 2>&1 | tee evidence/test-data/demo_race_condition_output.txt

# Borra contenedores Y volumen: el próximo `up` recrea esquema y seed desde cero.
# POR QUÉ existe: los .sql de initdb solo corren con volumen vacío.
clean:
	docker compose down -v --remove-orphans

# Reaplica el seed sobre una BD ya creada (limpia las tablas primero para no duplicar).
seed:
	docker compose exec -T postgres psql -U smartbancs -d smartbancs -c "TRUNCATE ledger_entries, outbox_events, ai_recommendations, transactions, accounts RESTART IDENTITY CASCADE"
	docker compose exec -T postgres psql -U smartbancs -d smartbancs -f /docker-entrypoint-initdb.d/03_seed.sql

# Tests unitarios (circuit breaker, con reloj simulado).
test-unit:
	docker compose --profile test run --rm -T tests pytest unit -v 2>&1 | tee evidence/test-data/test_unit_output.txt

# Apaga / enciende la IA para demostrar resiliencia a mano.
ai-down:
	docker compose stop ai-service

ai-up:
	docker compose start ai-service

# PRUEBA DECISIVA de la Fase 5: p95 de transferencias con la IA encendida, apagada y colgada.
ai-resilience:
	bash scripts/ai_resilience_demo.sh 2>&1 | tee evidence/ai-resilience/output.txt

# Pipeline ETL (Fase 6): genera el CSV sucio (si no existe), limpia, transforma y carga (Parquet + BD).
etl:
	docker compose up -d postgres
	docker compose --profile etl run --rm --build etl

# Ultimos lotes enviados a Bancs.
bancs-sync-log:
	docker compose exec -T postgres psql -U smartbancs -d smartbancs -c "SELECT batch_id, events_count, status, latency_ms, created_at FROM bancs_sync_log ORDER BY id DESC LIMIT 10"

# Bancs bajo carga: latencia y 503 al subir la concurrencia; lotes vs peticiones sueltas.
bancs-degradation:
	docker compose up -d bancs-mock
	docker compose --profile test run --rm -T tests python integration/bancs_degradation.py

# Integracion de extremo a extremo (requiere el stack completo: `make up`).
test-integration:
	docker compose up -d --build
	docker compose --profile test run --rm -T tests pytest integration -v 2>&1 | tee evidence/test-data/test_integration_output.txt

# Bancs se cae y vuelve: las transferencias siguen y el atraso se sincroniza solo.
bancs-resilience:
	bash scripts/bancs_resilience_demo.sh

# INCIDENTE 1 (Fase 7): deadlocks REALES con orden invertido vs cero con orden determinista.
deadlock-demo:
	docker compose up -d --build
	docker compose --profile test run --rm -T tests python incident/reproduce_deadlock.py 2>&1 | tee evidence/incident/deadlock_demo_console.txt
	-docker compose logs postgres 2>&1 | grep -B1 -A8 "deadlock detected" | head -40 > evidence/incident/postgres_log_deadlock.txt

# INCIDENTE 2 (Fase 7): agotamiento del pool por una transaccion larga; muestra deteccion y diagnostico.
exhaust-pool:
	docker compose up -d --build
	docker compose --profile test run --rm -T tests python incident/exhaust_pool.py

# Prueba de carga: rampa hasta saturacion sobre 5000 cuentas distintas.
load:
	bash scripts/load_test.sh

# Endpoints de diagnostico para el operador.
diagnostics:
	@echo "== pool"; curl -s localhost:8000/api/v1/admin/diagnostics/pool | python -m json.tool
	@echo "== blocking-tree"; curl -s localhost:8000/api/v1/admin/diagnostics/blocking-tree | python -m json.tool
	@echo "== locks"; curl -s localhost:8000/api/v1/admin/diagnostics/locks | python -m json.tool
	@echo "== slow-queries"; curl -s "localhost:8000/api/v1/admin/diagnostics/slow-queries?limit=5" | python -m json.tool

# Prueba unitaria de las reglas de alerta (necesita el contenedor de prometheus arriba).
alerts-test:
	docker compose up -d prometheus
	docker compose exec -T prometheus promtool test rules /etc/prometheus/alerts_test.yml
