#!/bin/bash
# Watchdog for task-bot. Keeps bot container running; notifies admin via TG.
# Backoff: after 3 consecutive failed up'ов — пауза 30 мин чтобы не плодить
# docker compose процессы при подвисшем docker daemon (был причиной fork-storm
# 10 июня 2026).
set -euo pipefail

PROJECT_DIR="/home/openclaw/workspace/task-bot"
LOG_FILE="/var/log/task-bot-watchdog.log"
STATE_FILE="/var/lib/task-bot-watchdog.state"
BACKOFF_FILE="/var/lib/task-bot-watchdog.backoff"
ENV_FILE="${PROJECT_DIR}/.env"
MAX_FAILS=3
BACKOFF_SEC=1800
DOCKER_TIMEOUT=30

mkdir -p "$(dirname "$STATE_FILE")"
touch "$LOG_FILE" "$STATE_FILE" 2>/dev/null || true

log() {
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE"
}

cd "$PROJECT_DIR"

# Docker inspect с таймаутом — если daemon подвис, не висим тут вечно
STATE=$(timeout "$DOCKER_TIMEOUT" docker inspect task-bot-bot-1 --format '{{.State.Status}}' 2>/dev/null || echo "docker-unreachable")
HEALTH=$(timeout "$DOCKER_TIMEOUT" docker inspect task-bot-bot-1 --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo "none")

LAST=$(cat "$STATE_FILE" 2>/dev/null || echo "")

if [ "$STATE" = "running" ] && { [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "none" ] || [ "$HEALTH" = "starting" ]; }; then
  if [ "$LAST" != "ok" ]; then
    log "bot recovered: state=$STATE health=$HEALTH"
    echo "ok" >"$STATE_FILE"
  fi
  rm -f "$BACKOFF_FILE"
  exit 0
fi

# Backoff check
NOW=$(date +%s)
FAILS=0
LAST_FAIL=0
if [ -f "$BACKOFF_FILE" ]; then
  FAILS=$(awk 'NR==1' "$BACKOFF_FILE" 2>/dev/null || echo 0)
  LAST_FAIL=$(awk 'NR==2' "$BACKOFF_FILE" 2>/dev/null || echo 0)
fi

if [ "$FAILS" -ge "$MAX_FAILS" ] && [ $((NOW - LAST_FAIL)) -lt "$BACKOFF_SEC" ]; then
  log "backoff active: $FAILS fails, $((NOW - LAST_FAIL))s since last — skipping recovery (next try in $((BACKOFF_SEC - NOW + LAST_FAIL))s)"
  exit 0
fi

# Сброс счётчика если backoff истёк
if [ $((NOW - LAST_FAIL)) -ge "$BACKOFF_SEC" ]; then
  FAILS=0
fi

log "bot down: state=$STATE health=$HEALTH — bringing up (fail #$((FAILS + 1)))"
if timeout "$DOCKER_TIMEOUT" docker compose up -d bot >>"$LOG_FILE" 2>&1; then
  UP_OK=1
else
  log "compose up failed (rc=$?)"
  UP_OK=0
fi

sleep 5
NEW_STATE=$(timeout "$DOCKER_TIMEOUT" docker inspect task-bot-bot-1 --format '{{.State.Status}}' 2>/dev/null || echo "docker-unreachable")
log "bot post-recovery: state=$NEW_STATE"
echo "recovered:$NEW_STATE" >"$STATE_FILE"

if [ "$NEW_STATE" = "running" ] && [ "$UP_OK" = "1" ]; then
  rm -f "$BACKOFF_FILE"
else
  FAILS=$((FAILS + 1))
  printf '%s\n%s\n' "$FAILS" "$NOW" >"$BACKOFF_FILE"
  if [ "$FAILS" -ge "$MAX_FAILS" ]; then
    log "backoff engaged: $FAILS consecutive fails, next try in $((BACKOFF_SEC / 60)) min"
  fi
fi

if [ -f "$ENV_FILE" ]; then
  BOT_TOKEN=$(grep '^BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  ADMIN_TG_ID=$(grep '^ADMIN_TG_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ADMIN_TG_ID:-}" ]; then
    MSG="watchdog: task-bot был ${STATE} (health=${HEALTH}), поднял. Текущий статус: ${NEW_STATE}"
    curl -sS -m 10 -X POST \
      "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      -d chat_id="${ADMIN_TG_ID}" \
      -d text="${MSG}" >>"$LOG_FILE" 2>&1 || log "tg notify failed"
  fi
fi
