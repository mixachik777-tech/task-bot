# task-bot

(описание проекта одной строкой)

## Сводка для Claude (читай перед началом сессии)

- **Стадия:** Идея (Идея / Активная разработка / Отладка / Готово / На паузе / Заблокировано)
- **Стек:** (стек)
- **Где код:** `~/workspace/task-bot/`
- **Wiki:** `~/knowledge/wiki/task-bot.md` (если есть)
- **Деплой:** локально (локально / xorek-сервер user-systemd / docker / прод-юнит)

## Что построено и работает

Перечисли реальные компоненты которые УЖЕ работают. Это первое что я должен увидеть при старте сессии.

- (пример) systemd-юнит `task-bot.service` (user) на основном сервере — статус `active`
- (пример) `app/main.py` — точка входа
- (пример) SQLite БД в `data/state.db`

## Endpoints / интеграции

API эндпоинты, allowlists, токены (только имена env-переменных, не значения).

- (пример) `POST /api/v1/foo` — обработка X
- (пример) `bot.api.telegram.org` через `${TG_BOT_TOKEN}`
- (пример) поддомен `task-bot.ivcurse.com` — прод-URL (поддомен домена ivcurse.com на nginx)

## Sibling-проекты (соседи в ~/workspace/)

Что брать из соседних проектов вместо переписывания. Сверяй с `~/workspace/CLAUDE.md` перед началом инфра-задач.

- (пример) `wb-sniper-relay/` — CF Worker для парсинга РФ-сайтов. Использовать через `https://wb-sniper-relay.<account>.workers.dev/proxy?url=...`
- (пример) `cf-worker-v2/` — VPN WS-tunneling, не путать с парсинг-relay

## Команды (как запускать)

```bash
# (пример) запуск локально
uv run python -m app.main

# (пример) деплой и рестарт на основном сервере
ssh openclaw@1557295.xorek.cloud 'systemctl --user restart task-bot'

# (пример) логи на сервере
ssh openclaw@1557295.xorek.cloud 'journalctl --user -u task-bot -f'
```

> **Терминология.** Сервер = `1557295.xorek.cloud` (хостер xorek.cloud). Домен = `ivcurse.com` с поддоменами (например `task-bot.ivcurse.com`). Не сливать в одно.

## Архитектура

Структура папки. Что где лежит. Принципы.

```
task-bot/
├── CLAUDE.md         # этот файл — основная сводка
├── DECISIONS.md      # архитектурные решения с обоснованием
├── FAILURES.md       # классы повторяющихся ошибок (soft-инжект в контекст)
├── BACKLOG.md        # что в работе и что в очереди
├── STAGE_NOTES.md    # лог по этапам
└── ...
```

## Конвенции и грабли (gotchas)

Локальные правила работы с проектом. То что не выводится из кода.

- (пример) Не запускать `wrangler deploy` без подтверждения — это прод-Worker для парсинга
- (пример) SQLite миграции через Alembic, не raw SQL

## Тестирование и верификация

Как проверять что фикс реально работает.

- (пример) `pytest tests/` — unit + integration
- (пример) `./scripts/smoke.sh` — end-to-end на staging

---

**Файлы-партнёры в этом проекте:**
- [DECISIONS.md](DECISIONS.md) — решения с обоснованием
- [FAILURES.md](FAILURES.md) — повторяющиеся ошибки, инжектится в контекст в начале сессии
- [BACKLOG.md](BACKLOG.md) — что делать
- [STAGE_NOTES.md](STAGE_NOTES.md) — журнал по этапам

Создано: 2026-06-11
