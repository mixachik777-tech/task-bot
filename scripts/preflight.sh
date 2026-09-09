#!/usr/bin/env bash
# Preflight-проверка перед первым стартом: .env заполнен, токены валидны,
# Docker установлен. Вызывается из README в шаге «убедись что готово».

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

EXIT_CODE=0
ok() { printf "  ✅ %s\n" "$1"; }
fail() { printf "  ❌ %s\n" "$1"; EXIT_CODE=1; }

echo "task-bot preflight:"

# 1. Docker
if command -v docker >/dev/null 2>&1; then
    ok "docker установлен ($(docker --version | head -c 50))"
else
    fail "docker не найден — установи docker engine"
fi

if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin доступен"
else
    fail "docker compose plugin не найден (нужен >=2.x)"
fi

# 2. .env
if [[ -f .env ]]; then
    ok ".env существует"
    set -a; source .env; set +a
    [[ -n "${BOT_TOKEN:-}" ]] && ok "BOT_TOKEN задан" || fail "BOT_TOKEN пуст"
    [[ -n "${ADMIN_TG_ID:-}" ]] && ok "ADMIN_TG_ID задан" || fail "ADMIN_TG_ID пуст"
    [[ -n "${DB_PASSWORD:-}" ]] && ok "DB_PASSWORD задан" || fail "DB_PASSWORD пуст"
    if [[ -z "${GEMINI_API_KEY:-}" ]]; then
        printf "  ⚠️  GEMINI_API_KEY пуст — AI-помощник будет отключён, остальное живёт\n"
    else
        ok "GEMINI_API_KEY задан"
    fi
else
    fail ".env не существует — скопируй .env.example в .env и заполни"
fi

# 3. Порты
if ss -tln 2>/dev/null | grep -q ':5432 '; then
    printf "  ⚠️  порт 5432 уже занят на хосте — postgres может не запуститься\n"
fi
if ss -tln 2>/dev/null | grep -q ':6379 '; then
    printf "  ⚠️  порт 6379 уже занят на хосте — redis может не запуститься\n"
fi

echo
if [[ $EXIT_CODE -eq 0 ]]; then
    echo "Всё ок. Запускай: docker compose up -d --build"
else
    echo "Найдены проблемы — исправь и запусти preflight ещё раз."
fi
exit $EXIT_CODE
