# STAGE_NOTES.md

Журнал по этапам автономного режима. Заполняется после закрытия каждого этапа.

## Этап 4: FSM создания задачи + админ-команды настройки супергруппы

- Создано файлов:
  - `DECISIONS.md`
  - `STAGE_NOTES.md`
  - `alembic/versions/b6f4a1e25c08_app_settings.py`
  - `app/db/repositories/app_settings.py`
  - `app/db/repositories/tasks.py`
  - `app/db/repositories/history.py`
  - `app/bot/utils/time.py`
  - `app/bot/utils/render.py`
  - `app/bot/keyboards/create_task.py`
  - `app/bot/keyboards/task_card.py`
  - `app/bot/states.py`
  - `app/services/task_service.py`
  - `app/bot/handlers/create_task.py`
  - `app/bot/handlers/task_stubs.py`
  - `tests/test_repositories_app_settings.py`
  - `tests/test_repositories_tasks.py`
  - `tests/test_time_utils.py`
  - `tests/test_render.py`

- Изменено файлов:
  - `app/db/models.py` (+ `AppSetting`)
  - `app/bot/middlewares/auth.py` (lookup в group без create)
  - `app/bot/handlers/admin.py` (+ `/set_chat`, `/set_topic`, `/set_leadership_topic`)
  - `app/bot/handlers/menu_stubs.py` (убран `BTN_CREATE_TASK`)
  - `app/main.py` (подключены `create_task_router`, `task_stubs_router`)

- Ключевые решения (см. `DECISIONS.md` → «Этап 4»):
  - TASK_CHAT_ID / LEADERSHIP_TOPIC_ID → таблица `app_settings(key, value)`
  - callback_data: `ct_<step>:<value>` для FSM, `task_<action>:<task_id>` для карточки задачи
  - AuthMiddleware расширен: lookup в group без create (закрывает соответствующий пункт BACKLOG БЛОК-3)
  - Создание задачи — одна транзакция БД, постинг в Telegram внутри: rollback на любом TG-сбое
  - БД-only режим, если TASK_CHAT_ID==0 или dept.topic_id==0 / LEADERSHIP_TOPIC_ID==0
  - Все datetime в БД — UTC, формат пользователю — `Europe/Moscow` через `format_dt_local`

- Smoke-test:
  - `docker compose up -d --build` → все контейнеры Up, миграция `b6f4a1e25c08 (head)` применена
  - Логи: `Running upgrade 838e62830e37 -> b6f4a1e25c08, app_settings`, сидер идемпотентен (3 отдела + админ остались, дубликатов нет)
  - `app_settings` пустая (TASK_CHAT_ID / LEADERSHIP_TOPIC_ID не настроены — ожидаемо до выполнения `/set_chat`)
  - pytest: **38 passed** (4 новых файла тестов: app_settings, tasks+history, time_utils, render)

- Известные ограничения:
  - Без `/set_chat` + `/set_topic` + `/set_leadership_topic` задачи создаются только в БД (warning в логе бота). Это «БД-only» режим из автономного промпта, ожидаемо.
  - Кнопки «✋ Принять в работу» / «✅ Завершить» / «💬 Комментарий» в карточках сейчас — заглушки, отвечают «Появится на Этапе 5».
  - FSM хранится в Redis (RedisStorage) → переживает рестарт бота, но при чистке Redis волей админа состояние теряется (стандартное поведение).
  - В preview-карточке нет визуальной превью самих файлов — показывается только счётчик. Для Этапа 4 достаточно; превью медиа можно добавить позже, если попросят.

- Открытые вопросы для пользователя:
  - Стиль кнопки «Без файлов / Готово» оставил как одну кнопку «Готово» — она работает и когда файлов нет (пользователь просто жмёт сразу), и когда уже есть. Если хочется явное разделение «Без файлов» / «Готово», скажи — переделаю на Этапе 5.
  - Кнопка «✏️ Начать заново» в preview сбрасывает FSM и стартует с первого шага. Альтернатива — редактирование отдельных полей по выбору. Сейчас простой re-start; обсуждаемо.

## Этап 5: Действия с задачей (Принять/Завершить/Отменить/Комментарий)

- Создано файлов:
  - `app/bot/utils/tg.py` — общие безопасные обёртки `send_with_retry`, `edit_text_safe`, `send_dm_safe`
  - `app/services/task_actions.py` — TaskActionsService (accept/complete/cancel/add_comment) + ActionResult dataclass + permission predicates
  - `app/bot/handlers/task_actions.py` — заменяет task_stubs.py: callback-хэндлеры task_accept/complete/cancel/comment + FSM TaskComplete.waiting_comment / TaskComment.waiting_text
  - `tests/test_task_actions_service.py` — 25 юнит-тестов помощников + permission-матрицы (без БД)

- Изменено файлов:
  - `app/db/repositories/tasks.py` (+ `lock_for_update`, `count_files`)
  - `app/bot/states.py` (+ `TaskComplete`, `TaskComment` группы)
  - `app/bot/keyboards/task_card.py` (+ кнопка «❌ Отменить» в NEW и IN_PROGRESS, + `build_complete_skip_kb`)
  - `app/services/task_service.py` (refactor: использует общий `send_with_retry` из utils/tg.py)
  - `app/main.py` (`task_stubs_router` → `task_actions_router`)
  - `tests/test_repositories_tasks.py` (+ тесты `lock_for_update`, `count_files`)
  - `DECISIONS.md` (+ блок «Этап 5»)
  - `BACKLOG.md` (см. ниже)

- Удалено файлов:
  - `app/bot/handlers/task_stubs.py` — заглушки полностью заменены реальной логикой

- Ключевые решения (см. `DECISIONS.md` → «Этап 5»):
  - SELECT FOR UPDATE через `TasksRepository.lock_for_update` — защита от двойного клика
  - Карточка отдела с клавиатурой, зеркало в «Руководстве» без (закреплено Правкой 2 Этапа 4)
  - dept-edit raises на ошибке (rollback), arch-edit best-effort (логируется WARNING)
  - Permission-матрица в callback-хэндлерах: accept=любой active, complete=assignee/admin, cancel=creator/admin, comment=creator/assignee/lead/admin
  - FSM TaskComplete/TaskComment — ввод комментария в том же топике, FSM key (chat,user)
  - Уведомления творцу/исполнителю — после commit, best-effort через `send_dm_safe`
  - `task_history.payload` форматы: completed → `{"result_comment": str|None}`; cancelled → `{"by_role": "creator"|"admin"}`; commented → `{"text": str, "from_user_role": str}`

- Smoke-test:
  - `docker compose up -d --build` → все контейнеры Up, миграция `b6f4a1e25c08 (head)` применена, бот стартует
  - pytest: **66 passed** (38 базовых + 3 новых tasks-repo + 25 task-actions helpers/perms)
  - Полный e2e через Telegram (создал → принял → завершил → отменил) — на ручную приёмку

- Известные ограничения:
  - **Service-level integration тесты не написаны.** TaskActionsService открывает свою сессию через `async_session_factory`, она изолирована от тестовой savepoint-сессии в conftest. Покрытие сделано через permission/transition unit-тесты + ручной smoke. Полные integration-тесты требуют либо session-injection в сервисе (рефактор интерфейса), либо отдельного conftest с TRUNCATE-cleanup. Зафиксировано в BACKLOG БЛОК-5.
  - Карточка задачи в БД-only режиме (без TASK_CHAT_ID) — `task.dept_chat_id` останется NULL → `_sync_card` no-op. Кнопки карточек тогда только через Telegram-сообщения (которых нет). Логика accept/complete/cancel в БД работает, но визуально не видна. Ожидаемо для smoke без супергруппы.
  - Permission-проверка для `cancel`/`complete` НЕ имеет дополнительного фильтра по отделу. Любой админ может отменить/завершить задачу любого отдела — это by design (admin = выше отделов).

- Открытые вопросы для пользователя:
  - Кнопка «❌ Отменить» добавлена в обе карточки (NEW и IN_PROGRESS). Permission-check в хэндлере, не в кнопке (кнопка видна всем, но клик отдаст alert). Ок?
  - Ввод комментария при «Завершить» — в том же топике. Альтернатива — переход в личку. Что лучше для UX?
  - На «Отменить» сейчас alert «Задача отменена», без явной причины. Стоит ли добавить FSM с обязательной причиной отмены? (Считаю — нет, отмена и так редкая операция.)

## Этап 6: APScheduler — напоминания + overdue + очистка топиков

- Создано файлов:
  - `app/bot/runtime.py` — глобальный accessor get_bot/set_bot
  - `app/scheduler/runtime.py` — accessor get_scheduler/set_scheduler
  - `app/scheduler/notifications.py` — Redis-dedup для overdue (TTL 24h) + DM helpers
  - `app/scheduler/jobs.py` — send_reminder, check_overdue, purge_completed_messages + 4 чистых helper'а
  - `app/scheduler/bootstrap.py` — bootstrap_scheduler + schedule_reminders_for_task + remove_reminders_for_task + EVENT_JOB_ERROR listener
  - `tests/test_scheduler_helpers.py` — 21 юнит-тест helper'ов

- Изменено файлов:
  - `app/main.py` — AsyncIOScheduler init + bootstrap + start; передача bot/redis в runtime'ы
  - `app/services/task_service.py` — после commit вызов schedule_reminders_for_task; обёртка БД-only режима через if/else (постинг и зеркало внутри `else`)
  - `app/services/task_actions.py` — _drop_reminders на complete и cancel (after commit, success-only)
  - `DECISIONS.md` (+ блок «Этап 6» с 11 решениями)
  - `BACKLOG.md` (+ БЛОК 6: 5 пунктов на Этап 7+)

- Ключевые решения (DECISIONS.md → «Этап 6»):
  - Per-task DateTrigger вместо interval-сканирования каждые 5 мин (отход от PROJECT_PLAN.md, согласовано)
  - `send_reminder` race: lock → флаг → commit → DM (after commit best-effort)
  - Redis dedup для overdue, TTL 24h (не 1ч из spec — было бы шумно)
  - Получатели overdue: leads данного отдела + ВСЕ admins
  - Purge только status='done' через 24ч от completed_at; cancelled живёт вечно
  - Recipients reminder'ов: status=in_progress → assignee, status=new → creator
  - `bot/runtime.py` + `scheduler/runtime.py` + `scheduler/notifications::set_redis` — глобальные accessors для job'ов

- Smoke-test:
  - `docker compose up -d --build` → бот стартует. В логах: `scheduler bootstrap: 0 active tasks → 0 reminder jobs scheduled` + `APScheduler стартовал` + `Запуск polling`. Это ожидаемо: tasks-таблица пустая (никаких задач не создано пока).
  - pytest: **87 passed** (66 прежних + 21 scheduler helpers)
  - Полный e2e (создать задачу с deadline через 5мин → дождаться h2-уведомления → завершить → убедиться что job снят → 24ч простоя → overdue DM) — на ручную приёмку

- Известные ограничения:
  - **Service-level integration тесты для job'ов не написаны** — тот же conftest-isolation issue, что и для TaskActionsService. Зафиксировано BACKLOG БЛОК-6.
  - При длинном даунтайме бота (>2ч) bootstrap НЕ компенсирует пропущенные h2-напоминания: timepoint в прошлом → пропуск. Решение в BACKLOG.
  - `purge_completed` держит транзакцию во время `bot.delete_message` (минорно — 6ч interval, мелкий объём задач). BACKLOG.
  - check_overdue последовательно шлёт DM (не gather) — на крупных группах leads/admin может тормозить. BACKLOG.

- Открытые вопросы для пользователя:
  - Тест полного e2e flow с per-task DateTrigger в реальной супергруппе — нужно `/set_chat`+`/set_topic`+`/set_leadership_topic` + создать задачу с близким deadline и дождаться. Готов проверить?
  - Job для покраски карточки в «🔴 Просрочена» прямо в топике (визуальный сигнал) — добавлять? Сейчас overdue только в DM. Backlog или сразу делать?

## Пост-Этап 6: правки по «Руководству» + 3-слойная защита polling + фикс «handler не отвечает»

### А. Доступ в закрытую тему «Руководство»

- Миграция `c1d2e3f4a5b6` — создана таблица `leadership_whitelist` (свой whitelist для записи в тему).
- Миграция `d2e3f4a5b6c7` — таблица **дропнута**. Решение: вместо своего whitelist полагаемся на штатный механизм Telegram — закрытый топик (`closeForumTopic`), куда могут писать только админы группы с правом `can_manage_topics`. Бот ничего не хранит про это и ничем не управляет.
- Команда `/lead_list` (admin-only) — показывает админов супергруппы через `bot.get_chat_administrators` с маркером `(manage topics)` и текстовой инструкцией «настройки группы → Администраторы → Добавить → выбрать → оставить только Управление темами».

### Б. 3-слойная защита от «тихой смерти» polling

Создано:
- `app/bot/heartbeat.py` — Redis-helpers `bump` / `age_seconds` для ключа `bot:heartbeat`, оба обёрнуты в `asyncio.wait_for(timeout=3)`
- `app/bot/middlewares/heartbeat.py` — `HeartbeatMiddleware`, регистрируется ПЕРВЫМ в цепочке
- `healthcheck.py` (в корне проекта) — Docker HEALTHCHECK-скрипт, читает ключ из Redis, exit 0 если возраст ≤ 180s

Изменено:
- `app/main.py`:
  - `_run_polling_supervised` — бесконечный retry-loop вокруг `dp.start_polling`, exponential ретрай уже есть в aiogram, наш — sleep 5s + restart на любом raise
  - `_watchdog` — таска параллельно, раз в 60s `bot.get_me()` с таймаутом 10s; на сбое cancel polling_task → supervisor рестартит
  - `dp.errors()` — глобальный handler, логирует любые исключения в хэндлерах через `logger.exception`
  - Перед `start_polling` теперь вызывается `bot.delete_webhook(drop_pending_updates=False)` — лечит self-conflict при рестарте контейнера (Telegram держит старую getUpdates-сессию ~30-60s)
- `Dockerfile` — добавлен `HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD python /app/healthcheck.py`
- `docker-compose.yml` — на `bot`-сервис повешен label `autoheal: "true"`, добавлен сервис `autoheal` (`willfarrell/autoheal:latest`) с интервалом 30s и start-period 60s

### В. Фикс «handler молча не отвечал»

Симптом: админ пишет `/start` в личку, heartbeat обновляется (значит middleware-цепочка достигнута), но бот ничего не отвечает. В логах ни одного исключения. APScheduler тикает нормально.

Корень: `AuthMiddleware.__call__` НЕ имел try/except. При любом сбое внутри (БД-таймаут, медленный pool, transient error) исключение пробивалось наружу, aiogram dispatcher молча скипал handler, в INFO-логах не появлялось ничего (aiogram логирует middleware-ошибки только на DEBUG). HeartbeatMiddleware шёл первым и успевал записать heartbeat — этим вводил в заблуждение при диагностике.

Фикс:
- `app/bot/middlewares/auth.py` — весь get-or-create блок обёрнут в `try/except Exception: logger.exception(...)`, после чего ВСЕГДА вызывается `handler(event, data)`. Если БД упала — хэндлер всё равно получит управление; те, кто требует `data["user"]`, упадут с понятной ошибкой через `dp.errors()`, видимой в логах.
- `app/bot/heartbeat.py::bump` — `redis.set` теперь в `asyncio.wait_for(timeout=3)`, чтобы залипший Redis не вешал middleware-цепочку.

### Г. Состояние

- Все 87 тестов проходят.
- БД: миграция `d2e3f4a5b6c7 (head)`, два пользователя — admin (540702488) и Алёна Шамина (409716754, role=admin).
- Контейнер `task-bot-bot-1` healthy, `task-bot-autoheal-1` healthy.

### Д. Открытые вопросы для следующего захода

- Перевести admin-команды (`/approve`, `/set_chat`, `/set_topic`, `/set_leadership_topic`, `/lead_list`) на кнопки — план согласован, ждёт отдельного go.
- Старт Этапа 7 (архив + детальная карточка + 🔴 индикатор + BACKLOG БЛОК-6).

### Е. Фикс «прикреплённые файлы не приходят в топик» → multi-format documents

Симптом: исполнитель получает карточку с «📎 Файлов: 3», но самих файлов в топике нет — скачать/открыть нечего.

Корень: `TaskService.create_task` сохранял файлы в `task_files` таблицу, но в Telegram не отправлял — только текст карточки.

Промежуточная итерация: ZIP-архив одним сообщением. Откатана по требованию пользователя — нужны мультиформат-документы, каждый файл отдельным сообщением, в оригинальном формате.

Финальное решение:
- `app/bot/utils/tg.py::send_file_with_retry` — всегда `bot.send_document`, никакого `send_photo`. Даже когда юзер прикрепил фото, в топике оно отображается как файл с иконкой «скачать», без inline-превью (так захотел заказчик: единый UX, файлы сохраняют оригинальный mime).
- `app/services/task_service.py::_post_files` — итерирует по списку files, шлёт каждый отдельным сообщением в тот же топик. Best-effort: единичный сбой логируется, остальные файлы продолжают идти; flood-retry-exhausted пробрасывается наружу для отката транзакции.
- Файлы отправляются и в топик отдела (с клавиатурой действий висит только на карточке), и в зеркало «Руководство».

`BACKLOG.md` — `message_id` файлов не сохраняются в БД → `purge_completed_messages` после `done`+24ч их не удалит. Миграция на Этапе 7+.

Транзакционное замечание: постинг файлов идёт ВНУТРИ `async with session.begin()`. Для MVP допустимо; на Этапе 7+ — вынести постинг за commit (best-effort после успешной БД-фиксации).

### Ж. Структурированное описание для отдела «Дизайнеры»

Цель: для задач в отдел «Дизайнеры» вместо одного free-text-описания собираем 4 структурированных поля.

Поля:
- 📌 Назначение (обязательно) — афиша / баннер / story / post / etc.
- 📐 Размеры (обязательно) — 1920×1080, А4, 1080×1920, etc.
- 📄 Форматы готового файла (обязательно) — JPG, PSD, AI, PDF, etc.
- 🎨 Референс (опционально) — текст/ссылка или кнопка «Пропустить».

Реализация:
- `app/bot/states.py::CreateTask` — добавлено 4 substate (`design_purpose`, `design_sizes`, `design_formats`, `design_reference`). Хранение в Redis FSMStorage переживает рестарт.
- `app/bot/keyboards/create_task.py::build_design_reference_kb` — новая клавиатура «Пропустить / ❌ Отменить».
- `app/bot/handlers/create_task.py`:
  - константа `DEPT_CODE_DESIGNERS = "designers"` + `DESIGN_FIELD_MIN_LEN=2, MAX=200, REFERENCE_MAX=500`.
  - в `cb_dept` теперь сохраняется `department_code` в FSM data (раньше — только id + name).
  - в `msg_deadline` ветвление: если `department_code == "designers"` → переход на `CreateTask.design_purpose` с подсказкой; иначе текущий `description` flow.
  - 4 новых хэндлера: `msg_design_purpose`, `msg_design_sizes`, `msg_design_formats`, `msg_design_reference` + `cb_design_ref_skip`.
  - валидация `_validate_design_field` — длина 2..200 для обязательных полей.
  - `_compose_design_description(data)` — склейка 4 полей в многострочный текст через `\n`, идущий в `tasks.description`.
  - В `_show_preview` и `cb_send`: если отдел дизайнеры — описание собираем из 4 полей, иначе берём `data["description"]`.
- `app/bot/utils/render.py::render_task_card` и `render_preview`: для description с `\n` выводим:
  ```
  📝 Описание:
  📌 Назначение: ...
  📐 Размеры: ...
  ```
  однострочные описания (для остальных отделов) — отрисовка как раньше.
- БД-схема НЕ менялась: всё хранится в существующей колонке `tasks.description`.
- Новые тесты в `tests/test_render.py`: `test_render_task_card_multiline_description`, `test_render_preview_multiline_description`. Итого 89/89 проходит.

### З. Навигация «⬅️ Назад» и «❌ Отменить» на всех шагах FSM

Симптом: на текстовых шагах (title, deadline, design_*) не было НИКАКИХ кнопок — юзер мог случайно ткнуть в reply-кнопку главного меню («📁 Архив» и т.п.), и это попадало в текст задачи. Из примера в чате: deadline-шаг получал «📁 Архив», парсер дат отвергал, юзер был в тупике.

Решение:
- `app/bot/keyboards/create_task.py` переписан:
  - Хелпер `_nav_rows(can_back)` — стандартные строки навигации.
  - Все шаги получают «❌ Отменить»; промежуточные — ещё «⬅️ Назад».
  - Константы callback_data вынесены: `CB_BACK`, `CB_CANCEL`, `CB_DESC_SKIP`, `CB_DESIGN_REF_SKIP`, `CB_FILES_DONE`, `CB_SEND`, `CB_EDIT`.
  - Новый билдер `build_text_step_nav_kb()` — клавиатура для шагов чисто с текстовым вводом (только Назад+Отменить).
- `app/bot/handlers/create_task.py`:
  - На 5 prompt'ах с текстовым вводом (после `cb_dept`, `cb_priority`, designer-старт после deadline, `msg_design_purpose`, `msg_design_sizes`) теперь `reply_markup=build_text_step_nav_kb()`.
  - Новая helper `_render_step_prompt(message, target_state, edit)` — централизованно рендерит prompt любого шага с его клавиатурой; используется хэндлером «Назад».
  - Новый callback `ct_back` → хэндлер `cb_back`: читает текущее состояние FSM, мапит на предыдущее (для `files` ветвится: для дизайнеров → `design_reference`, для остальных → `description`), вызывает `_render_step_prompt`.
  - Новый message-handler `msg_main_menu_during_fsm` — перехватывает текст кнопок главного меню (`BTN_ARCHIVE`, `BTN_CONTACTS`, `BTN_STATS`, `BTN_AI`, `BTN_CREATE_TASK`) во время FSM, отвечает «Сейчас идёт создание задачи. Сначала отмени.» с клавиатурой Назад+Отменить. Регистрируется ВЫШЕ обычных text-хэндлеров, чтобы перехватить раньше.
- Новый файл `tests/test_keyboards_create_task.py` — 7 тестов проверяют, что каждая клавиатура содержит правильные callback_data (Назад где надо, Отменить везде, Skip/Done где есть).
- Итого 96/96 проходит.

## Этап 7: Архив, контакты, статистика

- Создано файлов:
  - `app/db/repositories/analytics.py` — AnalyticsRepository.get_summary, period_bounds_utc, AnalyticsSummary dataclass
  - `app/bot/handlers/archive.py` — ArchiveBrowse FSM + список/фильтры/детальная карточка
  - `app/bot/handlers/contacts.py` — однократный render группированного списка
  - `app/bot/handlers/analytics.py` — AnalyticsBrowse FSM + переключатели scope/period
  - `tests/test_repositories_archive.py` — 7 тестов архивного listing
  - `tests/test_repositories_analytics.py` — 8 тестов аналитики (включая period_bounds_utc)

- Изменено файлов:
  - `app/db/repositories/tasks.py` — `list_archive`, `count_archive`, `get_full`, `list_assignees_with_archive`, helper `_archive_filters`
  - `app/db/repositories/users.py` — `list_active_with_department`, `list_by_ids`
  - `app/bot/utils/render.py` — overdue-ветка в `render_task_card`, новые функции `render_task_history` + `render_archive_row`, локализация payload
  - `app/bot/handlers/menu_stubs.py` — заглушки убраны для архива/контактов/статистики, остался только AI
  - `app/main.py` — подключены три новых роутера выше menu_stubs
  - `tests/test_render.py` — +8 тестов (overdue, render_task_history, render_archive_row)
  - `DECISIONS.md` — раздел «Этап 7»
  - `BACKLOG.md` — закрыты: тай-брейкер по id в истории, локализация payload в карточке, 🔴 индикатор в карточке отдела

- Ключевые решения (см. `DECISIONS.md` → «Этап 7»):
  - Архив и статистика через FSM, контакты — без FSM
  - Employee видит только свой отдел (зафиксировано на входе в архив, кнопка отдела скрыта)
  - Rolling 7d/30d для недели/месяца, today — от локальной полуночи MSK
  - `overdue` — current snapshot, не за период; `avg_completion_time` — только за период
  - scope `user` объединяет creator и assignee — одна полная картина «моих»
  - Архив фильтрует по `COALESCE(completed_at, created_at)` чтобы корректно работать с cancelled-без-completed_at
  - Overdue-индикатор в `render_task_card` — заменяет статусную строку, работает везде где рендер используется

- Smoke-test:
  - pytest: **121 passed** (96 прежних + 7 archive + 8 analytics + 8 render + period_bounds_utc через analytics — фактически 25 новых, 4 теста перекрывают друг друга через расширение test_render.py)
  - `docker compose up -d --build` — стек поднимается чисто; миграция `d2e3f4a5b6c7 (head)` применена; APScheduler стартовал; «Запуск polling (supervised)» в логе; ошибок нет

- Известные ограничения:
  - **Архив: при пустых данных кнопка пагинации `1/1` присутствует как «no-op»** (callback на ту же страницу). Не критично, но можно скрыть, если total==0. На MVP оставлено.
  - **Контакты не имеют пагинации** — на 200+ юзеров текст упрётся в Telegram-лимит 4096 символов. Для редакции (десятки сотрудников) — запас огромный. Решение на потом, не приоритет.
  - **Аналитика scope `user` для admin** показывает только его собственные задачи; нет UI «посмотреть статистику конкретного юзера». Это закрывают AI-инструменты на Этапе 8 (`get_user_workload`, `get_analytics(scope=user)` с подстановкой user_id из function call).
  - **Service-level integration тесты для handler'ов архива/контактов/аналитики не написаны** — та же проблема async_session_factory vs тестовая savepoint-сессия, как в БЛОК-5/6. Покрытие: репозитории + render через unit + ручной smoke в Telegram.

- Открытые вопросы для пользователя:
  - PNG-графики для статистики через matplotlib (упомянуты в PROJECT_PLAN как опциональные) НЕ реализованы — нужны? Если да, дам отдельный шаг с зависимостью.
  - В архиве пагинация по 10 строк. Если редакция растёт и архив вырастет до тысяч задач — можем добавить поиск по тексту/id или фильтр «по постановщику».
  - История событий в детальной карточке показывает ВСЁ. Если истории много (десятки комментариев), сообщение может стать длинным. Свернуть в «показать историю» через отдельный callback?

## Пост-Этап 7: Персональная продуктивность + PNG-графики

### А. Раздел «👤 Продуктивность сотрудника»

- В общую карточку «📊 Статистика» добавлена кнопка «👤 Продуктивность». Доступ — lead и admin.
- Для admin: выбор отдела → выбор сотрудника → карточка. Для lead: пропуск выбора отдела (фиксирован его собственный) → выбор сотрудника.
- Карточка отображается как PNG-фото с caption и inline-клавиатурой переключателя периода (today/week/month + «⬅️ К общей»).
- Состояние хранится в общем `AnalyticsBrowse.viewing` FSM с дополнительными data-ключами `an_prod_user`, `an_prod_period`.

### Б. AnalyticsRepository.get_user_productivity

- Новый dataclass `UserProductivity`: `assigned`, `completed`, `in_progress_now`, `overdue_now`, `avg_completion_seconds`, `daily_breakdown=[(date, assigned, completed)]`, границы периода UTC.
- `assigned` за период — `accepted_at` юзера в окне (момент принятия задачи в работу), а не `created_at` (т.к. при создании ассайни ещё нет — он назначается при accept).
- daily_breakdown — два запроса с `date_trunc('day', accepted_at AT TIME ZONE 'Europe/Moscow')` и аналогично для completed; Python склеивает в плотный массив дней без пропусков, чтобы график был ровным.
- Создал = постановщик НЕ показывается: задачи в task-bot ставит только руководитель (зафиксировано в memory `project_task_creation_role`).

### В. PNG-графика (matplotlib + DejaVu Sans)

- `app/bot/utils/charts.py::render_productivity_chart(daily, user_name, period_label) -> bytes`.
- matplotlib Agg backend (без GUI), figsize=8×4 dpi=130 → ~1040×520 PNG, DejaVu Sans (поставляется с matplotlib, кириллица читается).
- Группированный бар по дням: синий — назначено, зелёный — выполнено. Шаг x-меток адаптивный (1/2/3 дня в зависимости от размера периода).
- Пустой `daily` → канва с надписью «Нет данных за выбранный период».
- Отправка в TG: `BufferedInputFile(bytes, "productivity.png")` через `answer_photo` (новое сообщение) или `edit_media(InputMediaPhoto)` (переключение периода).
- Зависимости: `matplotlib>=3.9,<3.10` + `numpy>=1.26,<2.0` (numpy 2.x требует x86-64-v2, на сервере ivcurse только SSE2 — старый CPU). Заносим обе в `pyproject.toml`, `uv.lock` пересчитан.

### Г. UI-нюансы

- Переход «текст → фото» в одном чате нельзя сделать через `edit_text` → используется `message.delete() + answer_photo`. Внутри карточки переключатель периода — через `edit_media` (фото ↔ фото), c fallback на delete+send на исключении.
- При закрытии раздела (`an:close`) — то же: если текущее сообщение photo, удаляем и шлём текст «Статистика закрыта»; если text — обычный edit_text.

### Д. Состояние

- pytest: **128 passed** (+7 новых: 4 productivity, 3 charts smoke).
- Docker: matplotlib + numpy 1.26 встают в образ через uv sync; контейнер `task-bot-bot-1 Up (healthy)`, лог чистый, polling запущен.
- Все остальные сервисы (postgres, redis, autoheal) — healthy без изменений.

### Е. Открытые вопросы

- График сейчас отображает «по дням». Для длинного периода (month) 30 баров — терпимо, но узковато. Если жалоб не будет, оставлю; иначе свернуть в недели.
- Кнопка «👤 Продуктивность» доступна из общего меню статистики. Если попросят сделать её отдельным верхнеуровневым пунктом главного меню — отдельная правка.
- Цвета синий/зелёный — нейтральные. Можно подогнать под бренд клиента, если потребуется.

## Этап 8: AI-агент (Gemini 2.5 Flash через Google AI Studio)

- Создано файлов:
  - `app/ai/__init__.py`, `context.py`, `prompts.py`, `tools.py`, `tool_registry.py`, `agent.py`, `errors.py`
  - `app/db/repositories/ai_conversations.py`
  - `app/bot/handlers/ai_chat.py`
  - `tests/test_ai_repos_extensions.py`, `test_ai_tools.py`, `test_ai_agent_smoke.py`

- Изменено файлов:
  - `app/db/repositories/tasks.py` (+ `list_overdue`, `search_by_text`, импорт `timezone`)
  - `app/db/repositories/history.py` (+ `list_by_user_period`)
  - `app/bot/handlers/menu_stubs.py` (заглушка AI убрана — файл стал пустым router)
  - `app/main.py` (+ `ai_chat_router` подключён выше menu_stubs)

- Архитектурное решение (см. DECISIONS → «Этап 8»):
  - AI = изолированный read-only слой над существующими репозиториями.
  - Никаких новых миграций, никаких изменений в task_service / task_actions / scheduler / FSM создания задач.
  - tool-loop происходит ВНУТРИ одного `agent.ask()`, в `ai_conversations.messages` сохраняются только финальные user/model text-turn'ы. Это упрощает rehydration истории.
  - Модель: `gemini-2.5-flash` (на проекте Михаила квота 2.0-flash=0, 2.5-flash работает).
  - SDK: `google-genai` через `client.aio.models.generate_content` (native async, без `asyncio.to_thread`).

- 8 tool-функций (все read-only):
  - `get_user_tasks`, `get_user_workload`, `get_team_summary`, `get_overdue_tasks`,
    `find_tasks`, `get_task_details`, `get_user_history`, `find_users`.
  - Permission-логика внутри каждой: employee автоматически переключается на свой `user_id`, доступ к чужим задачам/командной сводке ограничен.

- Системный промпт — три блока: идентичность+off-topic-отбивка / факты проекта / runtime-контекст вызова. Жёсткое правило: посторонние вопросы → одна фраза «Я отвечаю только на вопросы про задачи редакции.» без извинений и альтернатив.

- UX:
  - Кнопка «🤖 Спросить AI» в главном меню. FSM `AiChat.viewing`, inline `[🗑 Очистить диалог] [✖️ Закрыть]`.
  - Лимит запроса 1000 символов, typing-indicator перед вызовом, ответы шлются с `parse_mode=None` (защита от срыва HTML-парса).
  - История 20 последних сообщений в `ai_conversations.messages` (JSONB), trim при append.

- Устойчивость:
  - Тайм-аут вызова 30s через `asyncio.wait_for` → AiTimeout → «Запрос занял слишком долго».
  - 429 → AiRateLimit → «Слишком много запросов, попробуй через минуту».
  - 5xx/network → AiUnavailable → «Ассистент временно недоступен».
  - max 5 итераций tool-loop → AiUnavailable.

- Smoke-test:
  - pytest: **173 passed** (148 прежних + 5 на расширения репозиториев + 15 на tools + 5 на agent).
  - `docker compose up -d --build` — стек поднимается, бот healthy, polling запущен.
  - Реального e2e теста с настоящим API в CI нет (экономим квоту 250 запросов/день). Smoke сделан через мок `_generate`.

- Известные ограничения:
  - История диалога хранит только финальные text-turn'ы (без tool-результатов). Модель «помнит», о чём говорила, но не помнит детали возвращённых инструментами данных. Для большинства запросов это нормально; если станет проблемой — переехать на сохранение полного tool-loop'а.
  - Векторный поиск (pgvector в БД заложен) НЕ задействован — substring-поиск `search_by_text` достаточен для редакции на десятки задач.
  - Rate-limit Gemini 2.5 Flash free: 10 RPM, 250 запросов/день — на 5 пользователей с запасом, отдельный дроссель не делали.

- Открытые вопросы для следующего захода:
  - Ручной e2e с реальным Gemini-вызовом (одного клика «🤖 Спросить AI» в TG + один вопрос — на ручную приёмку).
  - При появлении employee'ев в БД — проверить permission-логику в реальном UX, тесты unit покрыты.
  - Подумать про embedding-поиск для архива при росте задач до сотен.

## Этап 9: Коробочное решение для деплоя на новых VPS

- Создано файлов:
  - `Makefile` — преднастроенные команды (preflight, up, down, restart, logs, backup, restore, test, shell-db, shell-bot)
  - `scripts/preflight.sh` — проверка готовности окружения (Docker, .env, порты)
  - `scripts/backup.sh` — `pg_dump | gzip` + ротация (14 ежедневных + 4 еженедельных)
  - `scripts/restore.sh` — восстановление из дампа с подтверждением
  - `app/ai/quota.py` — per-user дневной rate-limit AI через Redis

- Изменено файлов:
  - `README.md` — полная пошаговая инструкция деплоя на чужой VPS: BotFather, супергруппа+топики, Gemini API, .env, set_chat/set_topic, /approve, команды админа, бэкапы, troubleshooting
  - `.env.example` — все поля с комментариями + новое `AI_USER_DAILY_LIMIT=30`
  - `app/config.py` — поле `AI_USER_DAILY_LIMIT: int = 30`
  - `app/bot/handlers/ai_chat.py` — проверка `check_and_increment` перед вызовом агента; при превышении лимита — сообщение с указанием квоты

- Ключевые решения (см. DECISIONS → «Этап 9»):
  - Бэкапы хранятся в локальной директории `./backups/` (а не в named volume), чтобы их легко было скачать `scp`.
  - Ротация ручная через `find` + проверка дня недели: 14 ежедневных, 4 еженедельных воскресных. Никакого внешнего инструмента типа `restic` — простой shell-скрипт, который клиент может прочитать.
  - Per-user лимит AI — Redis INCR + EXPIRE NX (атомарно), TTL до полуночи MSK. 0 = выключено.
  - Все деплой-скрипты — bash, не Python, чтобы запускались до того, как зависимости установлены.
  - Sentry SDK НЕ включаем: loguru-логи через `docker compose logs` достаточны на старте. Клиент может прикрутить отдельно.

- Smoke-test:
  - pytest: **173 passed** (без новых тестов на quota — он тривиальный INCR + проверка).
  - `make preflight`: успешно проходит, предупреждение про занятые порты ожидаемо (на машине разработки docker уже бегает на 5432/6379).
  - `make up`: контейнеры поднимаются, polling запускается чисто.

- Что осталось (не критично):
  - Тестовое восстановление из бэкапа в полностью чистой БД — не запускал на проде, чтобы не дёргать данные. Скрипты протестированы только синтаксически.
  - При желании можно добавить Sentry: dsn в .env, `sentry-sdk` в pyproject, `init` в main.py. На MVP пропущено.
  - В Makefile нет команды для миграции БД вручную — alembic-команды доступны через `docker compose exec bot alembic upgrade head`.

## Post-Этап 9: Личный кабинет

Цель: дать каждому сотруднику (включая employee) персональную страницу с собственными числами продуктивности и активными задачами по «температуре дедлайна». Раньше любая статистика была закрыта от рядовых.

### Что добавлено
- **Backend (`app/db/repositories/analytics.py`):**
  - `UserProductivity` расширен полями: `cancelled`, `on_time_count`, `completion_rate_pct`, `on_time_rate_pct`. Старые поля без изменений — обратная совместимость для `app/bot/handlers/analytics.py` и `app/ai/tools.py`.
  - Новый dataclass `ActiveTasksBuckets` с пятью корзинами (`overdue`, `burning_24h`, `today`, `this_week`, `later`).
  - `get_user_active_tasks(user_id)` — раскладка активных задач по корзинам (лесенкой). `selectinload(Task.department)` для deeplink.
  - `get_user_stale_tasks(user_id, stale_days=7)` — `in_progress` + последнее событие > N дней назад через коррелированный `MAX(task_history.created_at)`.
- **Backend (`app/db/repositories/tasks.py`):**
  - `list_unassigned(department_id=None, limit=50)` — `status='new' AND assignee_id IS NULL`, по всем отделам по умолчанию.
- **UI (`app/bot/handlers/cabinet.py`):**
  - Новый роутер `cabinet`. Кнопка `👤 Кабинет` в главном меню (всем ролям).
  - FSM `CabinetBrowse.viewing`, период во FSM-data, переключатель `today/week/month`.
  - Раскладка: шапка → цифры → активные по корзинам → залипшие → кнопка «🎯 Свободные» (отдельный экран со списком).
  - Deeplink на сообщение задачи в её топике через `<a href="https://t.me/c/...">#ID</a>`.
- **Меню (`app/bot/keyboards/main_menu.py`):** константа `BTN_CABINET`, кнопка добавляется ВСЕМ ролям. У lead/admin рядом остаётся `📊 Статистика`.
- **Регистрация роутера** в `app/main.py` после analytics_router.

### Тесты
- `test_analytics_productivity.py`: 9 тестов (4 новых) — on_time/completion_rate, cancelled через history, buckets layering, isolation per user, stale via history.
- `test_repositories_tasks.py`: 1 новый — `list_unassigned`.
- Полный прогон: 179 passed.

### Что НЕ сделано (см. BACKLOG)
- Утренний дайджест, weekly recap, Karma-индекс.
- AI-инструменты не расширены новыми полями `UserProductivity` (cancelled/on_time/completion_rate).
- Кэш на 60 сек в Redis.
- Push о залипшей задаче.

### Особенности и подводные камни
- `selectinload(Task.department)` обязателен в `get_user_active_tasks` и `get_user_stale_tasks`: без него попытка доступа к `task.department.name` падает в async с `MissingGreenlet` (lazy-load запрещён).
- Кнопка `🔄 Обновить` ловит `message not modified` на DEBUG-уровень: для кабинета это норма, alert юзеру не нужен.
- Корзина `today` обычно пуста (логически попадает в `burning_24h` если дедлайн в ближайшие 24ч). Намеренно: показываем, только если действительно остался редкий «хвост» сегодня после 24ч-окна.

---

## 2026-06-02: 3 инцидента — outbox / single-prompt / TG-БД рассинхрон

**Контекст:** жалобы редакции — «бот не отвечает», «не могу закрыть задачу», «после длинного описания тишина». В логах ERROR'ы вокруг `task_accept failed for #42` с `InvalidColumnReferenceError`.

### Инцидент 1 — outbox.enqueue_idempotent: партиальный индекс не совпал

**Симптом:** с 29 мая (4 дня) ни один пользователь не мог принять/завершить/отменить/переназначить/прокомментировать/согласовать/вернуть на доработку ни одну задачу. Все 7 action'ов TaskActionsService падали с `there is no unique or exclusion constraint matching the ON CONFLICT specification`, основная транзакция откатывалась, юзер видел alert «Ошибка. Попробуй ещё раз позже.».

**Причина:** миграция `g5h6i7j8k9l0_task_files_purpose` (26 мая) поменяла partial unique index `uq_outbox_task_action_active` с `WHERE status != 'failed'` на `WHERE status = 'pending'` (логически правильно — done-запись не должна блокировать новый pending). Синхронно код `OutboxRepository.enqueue_idempotent` обновлён не был, остался с `index_where=TaskOutbox.status != "failed"`. Postgres требует ТОЧНОГО совпадения предиката index_where с предикатом индекса.

**Фикс:** `app/db/repositories/outbox.py:33` — `index_where=TaskOutbox.status == "pending"`. Один символ.

**Правило в memory:** `feedback_partial_index_predicate_sync.md` — при смене `postgresql_where` индекса в миграции синхронно править все `on_conflict_do_*(index_where=...)` в коде.

### Инцидент 2 — Single-prompt FSM: «бот замер» после длинного текста

**Симптом:** при создании задачи (отдел designers) пользователь вставляет длинное ТЗ (~3500 символов) на шаге «Цель» дизайнерского подмастера, бот «не отвечает».

**Причина:** FSM `CreateTask` ведётся через одно «живое» prompt-сообщение, обновляемое `edit_message_text`. После длинного юзер-инпута TG-клиент НЕ скроллит к отредактированному сообщению — оно остаётся выше юзер-ответа и физически выезжает за экран. В FSM-state Redis всё ок (`design_format`), но юзер не видит шаг «Формат».

**Фикс:** `app/bot/handlers/create_task.py` — добавлен `force_new` параметр в `_show_step` и хелпер `_is_long(message)` (длина > 400 или ≥4 переносов). Если юзер прислал длинное — старый prompt удаляется, новый шлётся внизу чата. Применено ко всем текстовым шагам FSM. Дизайнерский подмастер ещё переразмечен на «Шаг 1/3 — Назначение», «Шаг 2/3 — Формат», «Шаг 3/3 — Референс» + явная преамбула, чтобы не путали с большим текстовым полем.

**Бонус:** `MAX_TITLE_LEN` поднят 255 → 1024. Миграция `m1n2o3p4q5r6_tasks_title_widen.py` расширила колонку.

**Правило в memory:** `feedback_single_prompt_after_long_input.md`.

### Инцидент 3 — TG/БД рассинхрон: legacy-карточки с фантомными кнопками

**Симптом:** «исполнитель видит задача в работе у меня, но не может сдать — alert "Отправлять отчёт может только исполнитель или админ"». БД говорит status=new, assignee=NULL.

**Причина:** `_sync_card(bot, task, ...)` (TG-побочка) сидела ВНУТРИ `async with session.begin():` блока, до `OutboxRepository.enqueue_idempotent`. Когда enqueue падал (инцидент 1), транзакция БД откатывалась, но `edit_message_text` в TG уже отработал — kb на карточке переключился на `in_progress`-вариант («✅ Завершить»), хотя в БД статус остался `new`. Юзер жал «Завершить» на фантомной кнопке, на сервере `_can_complete(task, user)` смотрел `task.assignee_id == NULL != user.id` → отказ.

**Срочное лечение:** скрипт `refresh_all_new.py` прошёл по всем `new`-задачам с broadcast'ами (4 шт × 5 чатов = 20 карточек) и `edit_message_text` синхронизировал kb с реальным БД-статусом.

**Структурный фикс:** в `app/services/task_actions.py` все 6 action'ов (accept/complete/cancel/approve/request_rework/reassign) переписаны на двухфазный паттерн:
- ВНУТРИ `session.begin()` — БД-операции + outbox enqueue + сбор `CardSyncSnap` (snapshot text/kb/chat_id/dept_msg_id/arch_msg_id).
- ПОСЛЕ commit'a — `_apply_card_snapshot(bot, snap, task_id)` best-effort, TG-ошибки логируются, не пробрасываются.

Старая функция `_sync_card` удалена. При любом будущем падении транзакции (новый рассинхрон индекса, deadlock, потеря коннекта) TG-сообщения НЕ успевают измениться — рассинхрон архитектурно невозможен.

**Правило в memory:** `feedback_tg_side_effects_after_commit.md`.

### Тесты
- Новый файл `tests/test_task_actions_e2e.py` — 3 e2e-сценария на реальной postgres через testcontainer + AsyncMock(Bot):
  - `test_full_cycle_accept_complete_approve` — accept → complete → approve, прямая регрессия на outbox-баг
  - `test_cancel_new_task_by_admin` — admin отменяет чужую `new`-задачу (сценарий @shaminaa_a)
  - `test_tg_edit_called_only_after_commit` — проверяет что `bot.edit_message_text` зовётся ровно 1 раз с правильными chat_id/message_id (регрессия на структурный фикс)
- Хак для тестов: `async_session_factory` патчится В namespace модуля `app.services.task_actions`, не только в `app.db.base` — `from app.db.base import async_session_factory` замораживает ссылку в namespace импортирующего модуля. Этот патч в conftest test_task_actions_e2e.py важен для всех будущих e2e-тестов сервисов.
- Полный прогон: **203 passed**.

### Особенности и подводные камни
- `display_number` в `TasksRepository._next_display_number` берётся как `MAX(display_number)+1` под `pg_advisory_xact_lock` — НЕ sequence. После rollback или DELETE значение переиспользуется, дыр в нумерации, видимых юзерам, не остаётся. Однако `tasks_id_seq` (внутренний `id`) прожигается обычным образом — юзеры этого не видят (id внутренний).
- Скрипт `refresh_all_new.py` следует держать на случай повторных рассинхронов любого происхождения: он идемпотентен, читает текущее состояние из БД и применяет его к TG.
- В тестах сервиса AsyncMock(Bot) достаточен — на все `await bot.X(...)` возвращается AsyncMock, реальных TG-вызовов нет. Помнить про патчинг `_try_update_lead_mirror` / `_try_purge_overdue` / `_drop_reminders` через `unittest.mock.patch` — они вызывают `asyncio.create_task` / APScheduler, которые в тестовом окружении не нужны.
