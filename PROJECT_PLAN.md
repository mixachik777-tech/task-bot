# PROJECT_PLAN.md — Telegram-бот для управления задачами редакции

Ты — senior-разработчик. Мы вместе будем создавать Telegram-бота для управления задачами редакции с интегрированным AI-ассистентом. Работаем строго по плану, поэтапно, без забегания вперёд.

═══════════════════════════════════════════════════════════════════
ГЛАВНЫЕ ПРАВИЛА РАБОТЫ
═══════════════════════════════════════════════════════════════════

1. Проект делится на 9 этапов (описаны в разделе "ПЛАН РАЗРАБОТКИ"). Сейчас выполняем ТОЛЬКО Этап 1. К следующему этапу не переходишь без моей явной команды "Этап N — поехали".

2. Перед написанием кода для каждого этапа — кратко опиши, что собираешься сделать, и дождись моего "ок". Это страховка от твоих фантазий.

3. После завершения каждого этапа покажи: дерево созданных/изменённых файлов, ключевые куски кода, инструкцию по проверке работоспособности.

4. Никаких "заодно я добавил...". Только то, что в этапе. Если видишь что-то полезное вне этапа — запиши в файл BACKLOG.md, обсудим потом.

5. Если в плане что-то непонятно или противоречиво — спроси меня, не додумывай.

6. Все секреты (токены, пароли, API-ключи) — только через .env, никогда в коде. В git коммитятся .env.example и .gitignore (содержащий .env).

7. Сохрани этот промпт целиком в файл PROJECT_PLAN.md в корне проекта. Это твоя единственная и постоянная справка по проекту — обращайся к нему перед каждым этапом.

═══════════════════════════════════════════════════════════════════
КОНТЕКСТ И НАЗНАЧЕНИЕ
═══════════════════════════════════════════════════════════════════

Цель проекта: Telegram-бот для постановки задач между отделами редакции (корректоры, дизайнеры, корреспонденты — расширяемо). Один общий чат-задачник (супергруппа Telegram с включёнными топиками/форумом), каждому отделу — свой топик. Отдельный закрытый топик "Руководство" служит зеркалом всех задач для руководителей.

Жёсткие архитектурные принципы:
— БД — единственный источник правды. Сообщения в Telegram — только зеркало для UX. Удаление сообщения из топика НИКОГДА не удаляет запись из БД.
— AI-агент встроен как НАДСТРОЙКА над детерминированным ядром, а не как фундамент. Кнопки "Принял"/"Завершить" работают через прямые SQL-обновления, не через LLM.
— Все действия пользователя логируются в task_history для аудита и контекста AI.
— Контроль доступа к топикам — на стороне Telegram (через права супергруппы). Бот не "защищает" — Telegram не покажет топик неприглашённому.

═══════════════════════════════════════════════════════════════════
ТЕХНОЛОГИЧЕСКИЙ СТЕК
═══════════════════════════════════════════════════════════════════

Язык:           Python 3.12
Бот:            aiogram 3.13+
БД:             PostgreSQL 16 + расширение pgvector (заложено на будущее под RAG)
Кеш/FSM:        Redis 7
ORM:            SQLAlchemy 2.0 async + Alembic
Планировщик:    APScheduler 3.x (внутри процесса бота)
AI:             Google Gemini 2.0 Flash через google-genai SDK (function calling, бесплатный tier)
Логи:           Loguru
Конфиг:         pydantic-settings + .env
Парсинг дат:    dateparser (RU)
Тесты:          pytest + pytest-asyncio + testcontainers
Деплой:         Docker Compose на VPS

═══════════════════════════════════════════════════════════════════
СТРУКТУРА ПРОЕКТА
═══════════════════════════════════════════════════════════════════

task-bot/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── README.md
├── PROJECT_PLAN.md         (этот документ)
├── BACKLOG.md              (идеи на потом)
├── alembic.ini
├── pyproject.toml
├── alembic/
│   ├── env.py
│   └── versions/
├── app/
│   ├── __init__.py
│   ├── main.py             точка входа, запуск бота + APScheduler
│   ├── config.py           pydantic-settings
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py         async engine, session factory
│   │   ├── models.py       все SQLAlchemy-модели
│   │   └── repositories/
│   │       ├── __init__.py
│   │       ├── tasks.py
│   │       ├── users.py
│   │       ├── departments.py
│   │       └── analytics.py
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── handlers/
│   │   │   ├── __init__.py
│   │   │   ├── start.py
│   │   │   ├── create_task.py
│   │   │   ├── task_actions.py
│   │   │   ├── analytics.py
│   │   │   ├── archive.py
│   │   │   ├── contacts.py
│   │   │   └── ai_chat.py
│   │   ├── keyboards/
│   │   │   └── __init__.py
│   │   ├── states.py
│   │   ├── middlewares/
│   │   │   ├── __init__.py
│   │   │   ├── auth.py
│   │   │   └── role.py
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── message_render.py
│   │       └── access.py
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── tools.py
│   │   └── prompts.py
│   ├── scheduler/
│   │   ├── __init__.py
│   │   ├── jobs.py
│   │   └── reminders.py
│   └── services/
│       ├── __init__.py
│       ├── task_service.py
│       └── notification_service.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_repositories.py
    ├── test_handlers.py
    └── test_ai_tools.py

═══════════════════════════════════════════════════════════════════
СХЕМА БАЗЫ ДАННЫХ (DDL для справки, реализация — через SQLAlchemy + Alembic)
═══════════════════════════════════════════════════════════════════

CREATE TABLE departments (
    id              BIGSERIAL PRIMARY KEY,
    code            VARCHAR(64) UNIQUE NOT NULL,
    name            VARCHAR(128) NOT NULL,
    topic_id        INTEGER NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    tg_user_id      BIGINT UNIQUE NOT NULL,
    tg_username     VARCHAR(64),
    full_name       VARCHAR(255) NOT NULL,
    role            VARCHAR(32) NOT NULL DEFAULT 'employee',
                    -- employee | lead | admin
    department_id   BIGINT REFERENCES departments(id),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE tasks (
    id                BIGSERIAL PRIMARY KEY,
    title             VARCHAR(255) NOT NULL,
    description       TEXT,
    priority          VARCHAR(16) NOT NULL,
                      -- low | medium | high | urgent
    deadline          TIMESTAMPTZ NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'new',
                      -- new | in_progress | done | cancelled
    creator_id        BIGINT NOT NULL REFERENCES users(id),
    assignee_id       BIGINT REFERENCES users(id),
    department_id     BIGINT NOT NULL REFERENCES departments(id),
    dept_chat_id      BIGINT,
    dept_message_id   INTEGER,
    arch_message_id   INTEGER,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    accepted_at       TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    reminder_2d_sent  BOOLEAN DEFAULT FALSE,
    reminder_1d_sent  BOOLEAN DEFAULT FALSE,
    reminder_2h_sent  BOOLEAN DEFAULT FALSE,
    is_message_purged BOOLEAN DEFAULT FALSE
);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_assignee ON tasks(assignee_id);
CREATE INDEX idx_tasks_dept_status ON tasks(department_id, status);
CREATE INDEX idx_tasks_deadline ON tasks(deadline) WHERE status IN ('new','in_progress');

CREATE TABLE task_files (
    id                BIGSERIAL PRIMARY KEY,
    task_id           BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tg_file_id        VARCHAR(255) NOT NULL,
    tg_file_unique_id VARCHAR(255),
    file_name         VARCHAR(255),
    file_size         INTEGER,
    mime_type         VARCHAR(128),
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE task_history (
    id          BIGSERIAL PRIMARY KEY,
    task_id     BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id     BIGINT REFERENCES users(id),
    event_type  VARCHAR(32) NOT NULL,
                -- created | accepted | completed | cancelled | commented | reassigned | deadline_changed
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_history_task ON task_history(task_id);

CREATE TABLE ai_conversations (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    messages    JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

═══════════════════════════════════════════════════════════════════
ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (.env.example)
═══════════════════════════════════════════════════════════════════

# Telegram
BOT_TOKEN=
ADMIN_TG_ID=
TASK_CHAT_ID=
LEADERSHIP_TOPIC_ID=

# Database
DB_HOST=postgres
DB_PORT=5432
DB_USER=taskbot
DB_PASSWORD=
DB_NAME=taskbot

# Redis
REDIS_HOST=redis
REDIS_PORT=6379

# AI
GEMINI_API_KEY=

# Misc
LOG_LEVEL=INFO
TZ=Europe/Moscow

═══════════════════════════════════════════════════════════════════
КАРТА ЭКРАНОВ
═══════════════════════════════════════════════════════════════════

Главное меню (/start в личке бота):
  [➕ Создать задачу]
  [📊 Статистика]      ← только для роли lead/admin
  [📁 Архив]
  [👥 Контакты]
  [🤖 Спросить AI]

FSM создания задачи (7 состояний):
  0. Выбор отдела (inline-кнопки из БД, только активные)
  1. Ввод названия (5–255 символов)
  2. Выбор важности (🟢 Низкая / 🟡 Средняя / 🟠 Высокая / 🔴 Срочная)
  3. Ввод срока (через dateparser RU: "завтра 18:00", "через 2 часа", "12.05 14:00")
  4. Описание (текст или "пропустить")
  5. Файлы (несколько, лимит 20 МБ через Bot API — при превышении внятное сообщение)
  6. Превью + [✅ Отправить] [✏️ Редактировать] [❌ Отменить]

При подтверждении:
  — INSERT tasks
  — POST в топик отдела → сохранить dept_message_id
  — POST копия в топик "Руководство" → сохранить arch_message_id
  — INSERT task_history (event_type='created')
  — Уведомить постановщика

Карточка задачи в топике отдела (формат сообщения):
  🆕 Задача #142
  📌 <название>
  🟠 Важность: Высокая
  ⏰ Срок: DD.MM.YYYY HH:MM (через Xд Yч)
  👤 Постановщик: @username
  📝 Описание: <текст>
  📎 Файлы: N
  ────────
  Статус: 🆕 Новая
  [✋ Принять в работу]

После "Принять":
  — Сообщение РЕДАКТИРУЕТСЯ (editMessageText), не пересоздаётся
  — Статус → "🟡 В работе: @ivanov"
  — Кнопки → [✅ Завершить] [💬 Комментарий]
  — task_history: 'accepted'
  — Защита от двойного клика: SELECT FOR UPDATE в одной транзакции
  — Уведомление постановщику

После "Завершить":
  — Бот запрашивает у исполнителя комментарий по результату (можно "пропустить")
  — Статус → "✅ Выполнено"
  — task_history: 'completed' с payload={result_comment}
  — Запускается отложенное удаление сообщения из топика отдела через 24ч
  — В топике "Руководство" сообщение остаётся НАВСЕГДА (там тоже редактируется до финального вида)
  — Уведомление постановщику

═══════════════════════════════════════════════════════════════════
AI-АГЕНТ
═══════════════════════════════════════════════════════════════════

Системный промпт (шаблон):

Ты — корпоративный AI-ассистент таск-трекера редакции.
Помогаешь сотрудникам и руководителям получать информацию о задачах,
сотрудниках и нагрузке, а также помогаешь корректно формулировать задачи.

ПРАВИЛА:
1. Никогда не выдумывай данные. Если для ответа нужна информация —
   используй инструменты. Если инструмент вернул пусто — так и скажи.
2. Уважай роли. Обычный сотрудник видит свои задачи и задачи своего отдела.
   Руководитель видит всё.
3. Для аналитических вопросов всегда используй get_analytics,
   а не угадывай из памяти.
4. Отвечай кратко, по делу. Числа подкрепляй фактами из инструментов.
5. Если пользователь просит "создать задачу" — НЕ создавай сам.
   Помоги сформулировать и предложи перейти к кнопке "Создать задачу".
6. Дату/время отвечай в формате DD.MM.YYYY HH:MM.

КОНТЕКСТ ВЫЗОВА:
- Текущий пользователь: {user_full_name} (id={user_id}, role={role}, dept={dept})
- Текущая дата/время: {now}

Инструменты (function calling, все принимают user_context для проверки прав):

  1. get_user_tasks(user_id, status=None) — задачи исполнителя
  2. get_department_tasks(dept_code, status=None, date_from=None, date_to=None)
  3. get_overdue_tasks(dept_code=None)
  4. get_analytics(scope, period) — scope: user|department|all; period: today|week|month
     Возвращает {created, completed, in_progress, overdue, avg_completion_time}
  5. get_user_workload(user_id) — текущая нагрузка + ближайшие дедлайны
  6. find_users(query, dept_code=None) — поиск по имени/нику
  7. get_task_details(task_id) — полная карточка + история
  8. suggest_task_formulation(rough_idea, dept_code)
     Возвращает {suggested_title, suggested_description, suggested_priority, clarifying_questions}

История диалога: хранится в ai_conversations, лимит контекста — последние 20 сообщений.
Обработка ошибок: при сбое Gemini — ответ "Ассистент временно недоступен, попробуй позже", бот продолжает работу.

═══════════════════════════════════════════════════════════════════
ПЛАНИРОВЩИК (APScheduler)
═══════════════════════════════════════════════════════════════════

Job 1: check_reminders (каждые 5 минут)
  — Найти задачи в статусах new/in_progress с приближающимся deadline
  — Пороги: за 2 дня, за 1 день, за 2 часа
  — Отправить ЛС исполнителю (если status='in_progress') или постановщику (если задача ещё new и горит)
  — Отметить флаги reminder_*_sent, чтобы не дублировать

Job 2: purge_completed_messages (каждую минуту)
  — SELECT tasks WHERE status='done' AND completed_at < NOW() - INTERVAL '24 hours' AND is_message_purged=FALSE
  — Для каждой: bot.delete_message(dept_chat_id, dept_message_id), обработать ошибки (сообщение могло быть удалено вручную)
  — SET is_message_purged=TRUE
  — Топик "Руководство" НЕ трогаем — там полный архив

Job 3: mark_overdue (каждые 10 минут)
  — Идентифицировать просроченные задачи
  — При первом обнаружении просрочки — нотификация руководителю отдела (если назначен)

═══════════════════════════════════════════════════════════════════
ОБРАБОТКА ОШИБОК И УСТОЙЧИВОСТЬ
═══════════════════════════════════════════════════════════════════

— FSM в Redis: переживает рестарт бота
— Отправка в топик не удалась → транзакция БД откатывается, пользователю сообщение "Ошибка отправки, попробуй ещё раз"
— Gemini API недоступен → fallback-сообщение, бот работает дальше
— Rate limit Gemini → retry с exponential backoff (max 3 попытки)
— Двойной клик "Принять" → SELECT FOR UPDATE, выигрывает первый, второму "Задача уже взята @ivanov"
— Файл > 20 МБ → отказ с понятным сообщением и предложением приложить ссылку на облако в описании
— Telegram flood control → aiogram сам ставит в очередь

═══════════════════════════════════════════════════════════════════
БЕЗОПАСНОСТЬ
═══════════════════════════════════════════════════════════════════

— Новый пользователь после /start в whitelist-режиме: видит "Аккаунт ожидает подтверждения"
— Админ командой /approve <tg_user_id> <department_code> <role> активирует
— RBAC через middleware: проверка роли перед каждым защищённым хэндлером
— Доступ к топикам — на стороне Telegram (права супергруппы)
— Секреты только в .env, .env в .gitignore
— GEMINI_API_KEY не логируется
— Логи задач только по ID, без содержимого
— PostgreSQL слушает только localhost (внутри docker-сети)

═══════════════════════════════════════════════════════════════════
ПЛАН РАЗРАБОТКИ (9 ЭТАПОВ)
═══════════════════════════════════════════════════════════════════

ЭТАП 1 — Скелет проекта
  • Структура папок по разделу "СТРУКТУРА ПРОЕКТА" (папки + __init__.py)
  • pyproject.toml со всеми зависимостями
  • Dockerfile (python:3.12-slim)
  • docker-compose.yml: postgres (pgvector/pgvector:pg16), redis:7, bot
  • .env.example + .gitignore
  • app/config.py через pydantic-settings
  • app/main.py: минимальный aiogram-бот, хэндлер /start отвечает "Бот запущен"
  • app/db/base.py: async engine + session factory
  • alembic init + env.py настроен под async + первая пустая миграция
  • README.md с инструкцией старта
  Критерий готовности: docker-compose up поднимается без ошибок; бот логирует старт; /start отвечает "Бот запущен"

ЭТАП 2 — Модели и репозитории
  • Все SQLAlchemy-модели из схемы БД
  • Миграция Alembic создаёт всю схему + включает расширение pgvector
  • Базовые репозитории: tasks, users, departments, analytics
  • Юнит-тесты репозиториев на testcontainers (postgres)
  Критерий: pytest проходит, схема применяется на чистой БД

ЭТАП 3 — Авторизация и onboarding
  • Middleware проверки регистрации (auth.py)
  • Middleware проверки роли (role.py)
  • Хэндлер /start: новый пользователь — в очередь, существующий — главное меню
  • Хэндлер /approve <tg_id> <dept_code> <role> — только для админа из ADMIN_TG_ID
  • Главное меню (рендер кнопок зависит от роли)
  • Сидер: один админ из ADMIN_TG_ID, три отдела из конфига (correctors/designers/correspondents)
  Критерий: админ может одобрить нового пользователя; меню показывает разные кнопки по ролям

ЭТАП 4 — FSM создания задачи
  • 7 состояний (states.py)
  • Хэндлеры в bot/handlers/create_task.py
  • Парсер дат на dateparser (RU локаль)
  • Загрузка файлов с проверкой размера
  • Превью с инлайн-кнопками подтверждения
  • При подтверждении: транзакция INSERT tasks + INSERT task_files + INSERT task_history + отправка в оба топика
  • Сохранение dept_message_id и arch_message_id
  Критерий: задача создаётся, видна в БД и в обоих топиках; FSM переживает рестарт бота

ЭТАП 5 — Действия с задачей
  • Callback "Принять" с защитой от двойного клика через SELECT FOR UPDATE
  • Callback "Завершить" с запросом комментария о результате
  • editMessageText для синхронизации карточки в обоих топиках
  • Нотификации постановщику (в личку)
  • Все события пишутся в task_history
  Критерий: полный цикл "создал → принял → завершил" работает; двойной клик защищён

ЭТАП 6 — Планировщик
  • APScheduler стартует вместе с ботом (один процесс)
  • Три job из раздела "ПЛАНИРОВЩИК"
  • Тесты с подкрученной системной датой/таймстампами в БД
  Критерий: задача с близким дедлайном получает напоминание; завершённая старше 24ч удаляется из топика отдела, но остаётся в "Руководстве" и в БД

ЭТАП 7 — Архив, контакты, аналитика
  • Меню "Архив": inline-навигация (пагинация, фильтр по отделу/исполнителю/датам)
  • Меню "Контакты": список сотрудников с фильтром по отделам
  • Меню "Статистика" (только lead/admin): текстовые сводки + опционально PNG-графики через matplotlib
  Критерий: все три раздела работают, данные совпадают с БД

ЭТАП 8 — AI-агент
  • Интеграция google-genai SDK
  • 8 инструментов из раздела "AI-АГЕНТ"
  • Системный промпт с подстановкой контекста пользователя
  • Хранение истории в ai_conversations, лимит 20 сообщений
  • Обработка ошибок API, retry с backoff
  • Тесты на 5 сценариев: "кто перегружен", "мои задачи", "просрочки", "помоги сформулировать", "статистика за неделю"
  Критерий: агент даёт корректные ответы на тестовых сценариях, не выдумывает данные

ЭТАП 9 — Полировка и деплой
  • README с полной инструкцией: настройка супергруппы (включение топиков, создание 4 топиков, добавление бота админом, копирование topic_id в .env)
  • Скрипт бэкапа PostgreSQL (pg_dump в /backups, ротация)
  • Опционально: Sentry SDK
  • Финальный smoke-тест на VPS
  Критерий: чистый деплой с нуля по README заводится за <15 минут

═══════════════════════════════════════════════════════════════════
ЧЕГО НЕ ДЕЛАЕМ (осознанно — не предлагай это)
═══════════════════════════════════════════════════════════════════

— Веб-интерфейс
— OAuth/SSO (Telegram сам аутентификация)
— RAG, векторный поиск, корпоративная wiki (pgvector заложен, но не используется)
— Многоязычность (только русский)
— Мобильное приложение
— Микросервисы, Kubernetes
— Внешние очереди (RabbitMQ/Kafka) — APScheduler + Redis покрывают масштаб

Если на этапе ловишь желание добавить что-то "полезное" сверху — записываешь в BACKLOG.md, не делаешь.
