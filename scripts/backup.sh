#!/usr/bin/env bash
# Бэкап PostgreSQL task-bot.
#
# Создаёт сжатый дамп в ./backups/taskbot-YYYY-MM-DD-HHMM.sql.gz.
# Ротация: оставляем последние 14 ежедневных + 4 еженедельных
# (последние 4 файла, созданные в воскресенье).
#
# Использование:
#   ./scripts/backup.sh            — разовый запуск
#   crontab -e:                    — добавь строку для ежедневного запуска в 03:00
#     0 3 * * * cd /opt/task-bot && ./scripts/backup.sh >> ./backups/backup.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="${PROJECT_DIR}/backups"
mkdir -p "$BACKUP_DIR"

cd "$PROJECT_DIR"

STAMP="$(date +%Y-%m-%d-%H%M)"
FILE="${BACKUP_DIR}/taskbot-${STAMP}.sql.gz"

echo "[$(date -Iseconds)] backup → ${FILE}"

# Дождаться готовности postgres (до 60 сек). docker.service может быть active,
# а контейнер ещё в "starting up" — pg_dump упадёт FATAL.
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U taskbot -d taskbot -q 2>/dev/null; then
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "[$(date -Iseconds)] ERROR: postgres not ready after 60s, aborting"
    exit 1
  fi
  sleep 2
done

docker compose exec -T postgres pg_dump -U taskbot -d taskbot --no-owner --clean --if-exists |
  gzip -9 >"$FILE"

SIZE=$(du -h "$FILE" | cut -f1)
echo "[$(date -Iseconds)] done, size=${SIZE}"

# ── Ротация ────────────────────────────────────────────────────────────
# Оставляем 14 последних ежедневных файлов.
# При set -euo pipefail `ls глоб 2>/dev/null` падает с exit 2, если файлов нет,
# и `2>/dev/null` глушит только stderr — exit code пропускается через pipefail
# и убивает скрипт. Защита через `|| true` на всём пайпе.
cd "$BACKUP_DIR"
{ ls -1t taskbot-*.sql.gz 2>/dev/null | tail -n +15 || true; } | while read -r old; do
  day_of_week=$(date -d "$(echo "$old" | sed -E 's/taskbot-([0-9-]+)-[0-9]{4}\.sql\.gz/\1/')" +%u 2>/dev/null || echo "")
  if [[ "$day_of_week" == "7" ]]; then
    WEEKLY_COUNT=$({ ls -1 taskbot-*-Sun.sql.gz 2>/dev/null || true; } | wc -l)
    if [[ "$WEEKLY_COUNT" -lt 4 ]]; then
      new_name="${old%.sql.gz}-Sun.sql.gz"
      mv -n "$old" "$new_name" && continue
    fi
  fi
  echo "[$(date -Iseconds)] rotate: rm ${old}"
  rm -f "$old"
done

# Оставляем максимум 4 еженедельных
{ ls -1t taskbot-*-Sun.sql.gz 2>/dev/null | tail -n +5 || true; } | while read -r old; do
  echo "[$(date -Iseconds)] rotate weekly: rm ${old}"
  rm -f "$old"
done

echo "[$(date -Iseconds)] retention applied"
