.PHONY: up down logs test clean seed test-concurrency demo-race

# Levanta todo en segundo plano y reconstruye la imagen si cambió el código.
up:
	docker compose up -d --build

# Detiene los contenedores CONSERVANDO los datos (volumen pgdata).
down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# Todos los tests (por ahora: la suite de concurrencia).
test: test-concurrency

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
