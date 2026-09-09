#!/usr/bin/env bash
# Восстановление PostgreSQL task-bot из бэкапа.
#
# Использование:
#   ./scripts/restore.sh ./backups/taskbot-2026-05-18-0300.sql.gz
#
# ВНИМАНИЕ: эта операция удалит текущие данные в БД (pg_dump делается с
# --clean --if-exists). Перед запуском убедись, что у тебя есть свежий
# бэкап и ты понимаешь, что восстанавливаешь.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Использование: $0 <файл-бэкапа.sql.gz>" >&2
    exit 1
fi

FILE="$1"
if [[ ! -f "$FILE" ]]; then
    echo "Файл не найден: ${FILE}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "ВНИМАНИЕ: текущая БД будет перезаписана содержимым ${FILE}"
echo "Для отмены нажми Ctrl+C в течение 5 секунд."
sleep 5

echo "[$(date -Iseconds)] restore ← ${FILE}"
gunzip -c "$FILE" | docker compose exec -T postgres psql -U taskbot -d taskbot

echo "[$(date -Iseconds)] done. Перезапусти бота: docker compose restart bot"
