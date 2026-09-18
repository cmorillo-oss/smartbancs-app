.PHONY: up down logs test clean seed

# Levanta todo en segundo plano y reconstruye la imagen si cambió el código.
up:
	docker compose up -d --build

# Detiene los contenedores CONSERVANDO los datos (volumen pgdata).
down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# Fase 1: solo verifica que la API responde. Se ampliará con pytest en fases siguientes.
test:
	curl -sf localhost:8000/health && echo
	curl -sf localhost:8000/ready && echo

# Borra contenedores Y volumen: el próximo `up` recrea esquema y seed desde cero.
# POR QUÉ existe: los .sql de initdb solo corren con volumen vacío.
clean:
	docker compose down -v --remove-orphans

# Reaplica el seed sobre una BD ya creada (limpia las tablas primero para no duplicar).
seed:
	docker compose exec -T postgres psql -U smartbancs -d smartbancs -c "TRUNCATE ledger_entries, outbox_events, ai_recommendations, transactions, accounts RESTART IDENTITY CASCADE"
	docker compose exec -T postgres psql -U smartbancs -d smartbancs -f /docker-entrypoint-initdb.d/03_seed.sql
