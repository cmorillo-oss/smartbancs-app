#!/usr/bin/env bash
# Prueba decisiva de la Fase 5: latencia p95 de las transferencias con la IA ENCENDIDA, APAGADA y COLGADA.
# Uso: bash scripts/ai_resilience_demo.sh       (o: make ai-resilience)
set -euo pipefail
cd "$(dirname "$0")/.."

run() { docker compose --profile test run --rm -T tests python integration/measure_ai_impact.py "$@"; }

wait_ready() {
  for _ in $(seq 1 40); do curl -sf localhost:8000/ready >/dev/null && return 0; sleep 2; done
  echo "la API no responde" >&2; return 1
}

# Reiniciar API y worker entre estados: pone el circuit breaker en CLOSED y limpia el estado en memoria,
# de modo que cada medición parte de las MISMAS condiciones iniciales.
reset_processes() { docker compose restart transaction-api outbox-worker >/dev/null; wait_ready; }

# Espera a que el worker entregue todos los eventos de IA pendientes (sistema en reposo antes de medir).
drain_backlog() {
  PSQLQ="docker compose exec -T postgres psql -U smartbancs -d smartbancs -tA"
  for _ in $(seq 1 60); do
    pending=$($PSQLQ -c "select count(*) from outbox_events where event_type='ai.transaction_created' and status in ('PENDING','PROCESSING')")
    [ "$pending" = "0" ] && { echo "  backlog de IA vacío"; return 0; }
    echo "  drenando backlog de IA: $pending pendientes..."; sleep 5
  done
}

docker compose up -d --build
docker compose start ai-service >/dev/null
wait_ready

# DISEÑO: condiciones ALTERNADAS (on, off, on, off, on, off) + una en "colgada". Una sola pasada por condición
# no sirve en esta máquina (todo corre en una VM de Docker Desktop): el ruido entre repeticiones iguales es
# mayor que el efecto a medir. Alternar y repetir reparte ese ruido por igual entre condiciones.
# Antes de CADA medición: IA arriba, backlog de eventos drenado y procesos reiniciados => mismo punto de partida.

measure_state() {   # $1 = etiqueta, $2 = up|stopped|paused
  docker compose start ai-service >/dev/null 2>&1 || true
  docker compose unpause ai-service >/dev/null 2>&1 || true
  drain_backlog
  case "$2" in
    stopped) docker compose stop ai-service >/dev/null ;;
    paused)  docker compose pause ai-service >/dev/null ;;
  esac
  reset_processes
  echo; echo "=== $1 (IA: $2) ==="
  if [ "$2" = "stopped" ] && [ ! -f evidence/ai-resilience/dns_check.txt ]; then
    docker compose exec -T transaction-api python -c "
import socket, time
t = time.perf_counter()
try: socket.getaddrinfo('ai-service', 8001)
except Exception as e: print('resolver ai-service (detenido): FALLA', type(e).__name__, 'tras %.2fs' % (time.perf_counter() - t))
" | tee evidence/ai-resilience/dns_check.txt
  fi
  run --label "$1"
}

for i in 1 2 3; do
  measure_state ai_on_$i up
  measure_state ai_off_$i stopped
done
measure_state ai_hung paused
docker compose unpause ai-service >/dev/null 2>&1 || true

# Modo BackgroundTasks (paso 10 literal del brief): una pareja, para documentar la diferencia.
docker compose start ai-service >/dev/null 2>&1; drain_backlog
AI_NOTIFY_IN_PROCESS=true docker compose up -d transaction-api >/dev/null; wait_ready
docker compose restart outbox-worker >/dev/null
echo; echo "=== ai_inproc_on (IA: up, la API notifica en BackgroundTasks) ==="; run --label ai_inproc_on
docker compose stop ai-service >/dev/null; reset_processes
echo; echo "=== ai_inproc_off (IA: stopped, la API notifica en BackgroundTasks) ==="; run --label ai_inproc_off
docker compose start ai-service >/dev/null
AI_NOTIFY_IN_PROCESS=false docker compose up -d transaction-api >/dev/null; wait_ready   # restaura el modo por defecto

echo; run --compare || true

echo; echo "=== RECUPERACIÓN: la IA vuelve y el worker entrega los eventos acumulados ==="
PSQL="docker compose exec -T postgres psql -U smartbancs -d smartbancs -tA"
for i in $(seq 1 45); do
  pending=$($PSQL -c "select count(*) from outbox_events where event_type='ai.transaction_created' and status in ('PENDING','PROCESSING')")
  echo "  t+$((i*4))s  eventos de IA pendientes: $pending"
  [ "$pending" = "0" ] && break
  sleep 4
done
{
  echo "Estado final de los eventos ai.transaction_created:"
  $PSQL -c "select status, count(*) from outbox_events where event_type='ai.transaction_created' group by 1 order by 1"
  echo "Recomendaciones almacenadas: $($PSQL -c 'select count(*) from ai_recommendations')"
} | tee evidence/ai-resilience/recovery.txt
