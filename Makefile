.PHONY: help preflight up down restart logs ps build backup restore lint smoke test shell-db shell-bot

help:
	@echo "task-bot — команды управления"
	@echo
	@echo "  make preflight   проверить готовность (Docker, .env, порты)"
	@echo "  make up          собрать и запустить стек"
	@echo "  make down        остановить и удалить контейнеры (данные БД сохраняются)"
	@echo "  make restart     перезапустить только бота (без БД/Redis)"
	@echo "  make logs        живой лог бота (Ctrl+C для выхода)"
	@echo "  make ps          статус контейнеров"
	@echo "  make build       пересобрать образ бота без запуска"
	@echo "  make backup      создать дамп PostgreSQL в ./backups/"
	@echo "  make restore F=./backups/имя.sql.gz   восстановить из бэкапа"
	@echo "  make lint        прогнать ruff (статический lint-gate)"
	@echo "  make smoke       прогнать smoke-import всех app-модулей"
	@echo "  make test        lint + smoke + pytest"
	@echo "  make shell-db    psql внутрь контейнера postgres"
	@echo "  make shell-bot   bash внутрь контейнера бота"

preflight:
	@./scripts/preflight.sh

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart bot

logs:
	docker compose logs -f bot

ps:
	docker compose ps

build:
	docker compose build

backup:
	@./scripts/backup.sh

restore:
	@if [ -z "$(F)" ]; then echo "Использование: make restore F=./backups/имя.sql.gz"; exit 1; fi
	@./scripts/restore.sh "$(F)"

lint:
	.venv/bin/python -m ruff check app/ tests/

smoke:
	.venv/bin/python scripts/smoke_imports.py

test: lint smoke
	.venv/bin/python -m pytest -q

shell-db:
	docker compose exec postgres psql -U taskbot -d taskbot

shell-bot:
	docker compose exec bot bash
