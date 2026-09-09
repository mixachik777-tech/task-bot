"""
Главное меню — ReplyKeyboardMarkup, рендерится по роли пользователя.

Константы текстов кнопок переиспользуются фильтрами в menu_stubs.py
(F.text == BTN_*) — это страховка от рассинхронизации текста в клавиатуре
и в фильтре.
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.db.enums import UserRole

BTN_CREATE_TASK = "➕ Создать задачу"
BTN_ARCHIVE = "📁 Архив"
BTN_TEAM = "👥 Состав"
BTN_STATS = "📊 Статистика"
BTN_CABINET = "👤 Кабинет"
BTN_AI = "🤖 Спросить AI"

# Версия раскладки reply-клавиатуры главного меню.
# Telegram кеширует ReplyKeyboardMarkup на стороне клиента — старая клавиатура
# остаётся видимой, пока бот не пришлёт сообщение с новым reply_markup.
# MenuAwareBot (app/bot/menu_refresh.py) сравнивает эту константу с Redis-меткой
# на пользователя и при расхождении автоматически прицепляет свежее меню к
# следующему отправленному в личку сообщению. Поднимай константу при любом
# изменении состава/порядка кнопок в build_main_menu.
MENU_VERSION = "2026-05-25-create-for-all"


def build_main_menu(role: UserRole) -> ReplyKeyboardMarkup:
    """
    Главное меню под роль.

    «➕ Создать задачу» — у всех активных. С 2026-05-25 постановка
    задач открыта всем участникам, не только руководителям: задача
    кидается в общий пул отдела, конкретного исполнителя постановщик
    не выбирает, поэтому власти над коллегами это не даёт.
    «📊 Статистика» — только lead/admin (сводки по отделу/всем).
    «👤 Кабинет» — у всех: личная продуктивность, активные задачи.
    Архив и Состав доступны всем активным.
    """
    rows: list[list[KeyboardButton]] = []
    is_manager = role in {UserRole.LEAD, UserRole.ADMIN}
    rows.append([KeyboardButton(text=BTN_CREATE_TASK)])
    if is_manager:
        rows.append([KeyboardButton(text=BTN_CABINET), KeyboardButton(text=BTN_STATS)])
    else:
        rows.append([KeyboardButton(text=BTN_CABINET)])
    rows.append([KeyboardButton(text=BTN_ARCHIVE), KeyboardButton(text=BTN_TEAM)])
    rows.append([KeyboardButton(text=BTN_AI)])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )
