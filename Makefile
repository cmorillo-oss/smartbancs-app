.PHONY: up down logs test clean seed test-concurrency demo-race test-unit ai-down ai-up ai-resilience

# Levanta todo en segundo plano y reconstruye la imagen si cambió el código.
up:
	docker compose up -d --build

# Detiene los contenedores CONSERVANDO los datos (volumen pgdata).
down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# Todos los tests (por ahora: la suite de concurrencia).
test: test-unit test-concurrency

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
