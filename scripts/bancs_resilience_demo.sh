#!/usr/bin/env bash
# Demo: Bancs (el legado) se cae. Las transferencias siguen funcionando, los cambios se acumulan en el
# outbox, y cuando Bancs vuelve se sincronizan solos. Uso: bash scripts/bancs_resilience_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PSQL="docker compose exec -T postgres psql -U smartbancs -d smartbancs -tA"
OUT=evidence/bancs/resilience.txt
mkdir -p evidence/bancs

say() { echo "$@" | tee -a "$OUT"; }
transfers() {  # $1 = cuántas; imprime cuántas respondieron 201 y la latencia media
  local ok=0 total_ms=0
  for i in $(seq 1 "$1"); do
    out=$(curl -s -o /dev/null -w "%{http_code} %{time_total}" -H Content-Type:application/json \
      -H "Idempotency-Key: bancs-demo-$RANDOM-$RANDOM-$i" \
      -d '{"source_account":"ACC-000003","dest_account":"ACC-000004","amount":1,"currency":"USD"}' localhost:8000/api/v1/transactions)
    [ "${out%% *}" = "201" ] && ok=$((ok+1))
  done
  echo "$ok/$1 transferencias OK (201)"
}
pending() { $PSQL -c "select count(*) from outbox_events where event_type='bancs.balance_updated' and status in ('PENDING','PROCESSING')"; }
breaker() { curl -s localhost:9100/metrics 2>/dev/null | grep "^smartbancs_bancs_circuit_breaker_state" || docker compose exec -T outbox-worker python -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:9100/metrics').read().decode().splitlines() if l.startswith('smartbancs_bancs_circuit_breaker_state')][0])"; }

: > "$OUT"
say "=== Demo: caída del core legado Bancs ($(date -u +%FT%TZ)) ==="
docker compose up -d >/dev/null
for _ in $(seq 1 30); do curl -sf localhost:8000/ready >/dev/null && break; sleep 2; done

say ""; say "1) Estado inicial (Bancs encendido):"
say "   $(transfers 5)"; sleep 5; say "   cambios pendientes de sincronizar: $(pending)"

say ""; say "2) Apagamos Bancs (docker compose stop bancs-mock) y hacemos 40 transferencias:"
docker compose stop bancs-mock >/dev/null
say "   $(transfers 40)   <- la API NO depende de Bancs"
sleep 12
say "   cambios acumulados en el outbox: $(pending)"
say "   $(breaker)   (0=CLOSED 1=OPEN)"
say "   últimos lotes en bancs_sync_log:"
$PSQL -c "select status, events_count, coalesce(left(error_message,60),'') from bancs_sync_log order by id desc limit 3" | sed 's/^/     /' | tee -a "$OUT"
say "   eventos en DEAD (dead letter): $($PSQL -c "select count(*) from outbox_events where status='DEAD'")"

say ""; say "3) Encendemos Bancs otra vez y esperamos a que el worker envíe el atraso:"
docker compose start bancs-mock >/dev/null
for i in $(seq 1 30); do
  p=$(pending); say "   t+$((i*4))s pendientes: $p"; [ "$p" = "0" ] && break; sleep 4
done
say "   $(breaker)"

say ""; say "4) Conciliación final (saldo local vs Bancs) de las cuentas usadas:"
curl -s "localhost:8000/api/v1/admin/reconciliation?accounts=ACC-000003,ACC-000004" | python -c "
import sys, json
d = json.load(sys.stdin); print('   resumen:', d['summary'])
for i in d['items']: print('  ', i['account_number'], 'local', i['local_balance'], 'bancs', i['bancs_balance'], i['result'])" | tee -a "$OUT"
