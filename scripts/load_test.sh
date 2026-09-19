#!/usr/bin/env bash
# Prueba de carga completa: prepara 5000 cuentas, mide CPU de los contenedores, ejecuta Locust y VERIFICA
# que bajo carga no se perdió ni se creó dinero. Uso: bash scripts/load_test.sh   (o: make load)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=evidence/load-test-results
mkdir -p "$OUT"
PSQL="docker compose exec -T postgres psql -U smartbancs -d smartbancs -tA"

docker compose up -d --build >/dev/null
for _ in $(seq 1 40); do curl -sf localhost:8000/ready >/dev/null && break; sleep 2; done

# Sistema en reposo antes de medir: el backlog de eventos (IA/Bancs) de pruebas anteriores compite por la BD.
echo "Esperando a que el outbox esté vacío..."
for _ in $(seq 1 60); do
  p=$($PSQL -c "select count(*) from outbox_events where status in ('PENDING','PROCESSING')")
  [ "$p" = "0" ] && break; sleep 5
done

echo "Preparando 5000 cuentas de prueba (saldo 1.000.000 cada una)..."
$PSQL -c "INSERT INTO accounts (account_number, customer_id, balance, currency)
          SELECT 'LT-' || lpad(g::text, 5, '0'), 'CUST-LT-' || lpad(g::text, 5, '0'), 1000000, 'USD' FROM generate_series(1, 5000) g
          ON CONFLICT (account_number) DO UPDATE SET balance = 1000000, status = 'ACTIVE'" >/dev/null
BEFORE=$($PSQL -c "select sum(balance) from accounts where account_number like 'LT-%'")
TX_BEFORE=$($PSQL -c "select count(*) from transactions")
DL_BEFORE=$($PSQL -c "select deadlocks from pg_stat_database where datname='smartbancs'")

# Muestreo de CPU/memoria de los contenedores cada 2 s (100% = 1 núcleo): dice QUÉ se satura.
: > "$OUT/docker_stats.csv"
( while true; do docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' 2>/dev/null | sed "s/^/$(date +%s),/" >> "$OUT/docker_stats.csv"; sleep 1; done ) &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

echo "Ejecutando la rampa de carga (8 escalones x 25 s = ~3.5 min)..."
docker compose --profile test run --rm -T tests locust -f load/locustfile.py --headless --host http://transaction-api:8000 \
  --csv /evidence/load-test-results/locust --html /evidence/load-test-results/locust_report.html --only-summary 2>&1 \
  | grep -v "^ Container\|^#" | tee "$OUT/locust_console.txt" | sed -n '/PRUEBA DE CARGA/,$p' || true
# `|| true`: Locust termina con código != 0 cuando hubo peticiones fallidas, y en una prueba que sube hasta
# SATURAR eso es lo ESPERADO. Sin esto `pipefail` abortaba el script aquí y la verificación nunca se ejecutaba
# (error real que dejó un proceso de espera girando más de 20 minutos).
kill $SAMPLER 2>/dev/null || true

sleep 3
AFTER=$($PSQL -c "select sum(balance) from accounts where account_number like 'LT-%'")
TX_AFTER=$($PSQL -c "select count(*) from transactions")
DL_AFTER=$($PSQL -c "select deadlocks from pg_stat_database where datname='smartbancs'")
DEBIT=$($PSQL -c "select coalesce(sum(amount),0) from ledger_entries where entry_type='DEBIT'")
CREDIT=$($PSQL -c "select coalesce(sum(amount),0) from ledger_entries where entry_type='CREDIT'")

{
  echo
  echo "VERIFICACIÓN DE CORRECCIÓN BAJO CARGA"
  echo "====================================="
  echo "Dinero total en las 5000 cuentas:  antes $BEFORE  |  después $AFTER   ->  $([ "$BEFORE" = "$AFTER" ] && echo 'CONSERVADO (ni un centavo perdido ni creado)' || echo 'DESCUADRADO')"
  echo "Transferencias registradas en la carga: $((TX_AFTER - TX_BEFORE))"
  echo "Libro mayor: suma DEBIT = $DEBIT  |  suma CREDIT = $CREDIT   ->  $([ "$DEBIT" = "$CREDIT" ] && echo 'CUADRA' || echo 'DESCUADRADO')"
  echo "Deadlocks de PostgreSQL durante la carga: $((DL_AFTER - DL_BEFORE))"
  echo
  echo "USO DE CPU DURANTE LA CARGA (100% = un núcleo completo) — pico y media de cada contenedor:"
  python scripts/cpu_summary.py "$OUT/docker_stats.csv"
} | tee "$OUT/verification.txt"
