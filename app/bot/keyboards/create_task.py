"""Инлайн-клавиатуры FSM создания задачи."""

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import Department

PRIORITY_BUTTONS: list[tuple[str, str]] = [
    ("🟢 Низкая", "low"),
    ("🟡 Средняя", "medium"),
    ("🟠 Высокая", "high"),
    ("🔴 Срочная", "urgent"),
]

CB_BACK = "ct_back"
CB_CANCEL = "ct_cancel"
CB_DESC_SKIP = "ct_desc_skip"
CB_DESC_SKIP_CONFIRM = "ct_desc_skip_confirm"
CB_DESC_SKIP_BACK = "ct_desc_skip_back"
CB_DESIGN_REF_SKIP = "ct_design_ref_skip"
CB_DESIGN_FORMAT_CUSTOM = "ct_design_fmt_custom"
CB_FILES_DONE = "ct_files_done"
CB_SEND = "ct_send"
CB_EDIT = "ct_edit"

DESIGN_FORMAT_BUTTONS: list[tuple[str, str]] = [
    ("🟦 Квадрат", "Квадрат"),
    ("▭ Горизонтальный", "Горизонтальный"),
    ("▮ Вертикальный", "Вертикальный"),
]


def _nav_rows(can_back: bool) -> list[list[InlineKeyboardButton]]:
    """Стандартный набор навигации в нижней части любого FSM-шага."""
    rows: list[list[InlineKeyboardButton]] = []
    if can_back:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_BACK)])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=CB_CANCEL)])
    return rows


def build_dept_kb(depts: Sequence[Department]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=d.name, callback_data=f"ct_dept:{d.id}")] for d in depts]
    rows.extend(_nav_rows(can_back=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_priority_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"ct_pri:{val}")]
        for label, val in PRIORITY_BUTTONS
    ]
    rows.extend(_nav_rows(can_back=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_text_step_nav_kb() -> InlineKeyboardMarkup:
    """Клавиатура для шагов с чисто текстовым вводом (title, deadline, design_*)."""
    return InlineKeyboardMarkup(inline_keyboard=_nav_rows(can_back=True))


def build_description_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🚫 Описания не требуется", callback_data=CB_DESC_SKIP)]]
    rows.extend(_nav_rows(can_back=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_description_skip_confirm_kb() -> InlineKeyboardMarkup:
    """Двухшаговое подтверждение «без описания».

    Юзер нажал «🚫 Описания не требуется» — спрашиваем уверен ли:
    «✅ Да, без описания» (callback CB_DESC_SKIP_CONFIRM) либо
    «⬅️ Назад, напишу» (CB_DESC_SKIP_BACK).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, без описания",
                    callback_data=CB_DESC_SKIP_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад, напишу",
                    callback_data=CB_DESC_SKIP_BACK,
                )
            ],
        ]
    )


def build_design_format_kb() -> InlineKeyboardMarkup:
    """Шаг «Формат» для дизайнерских задач: 3 готовых соотношения + свой."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=label, callback_data=f"ct_design_fmt:{i}")]
        for i, (label, _) in enumerate(DESIGN_FORMAT_BUTTONS)
    ]
    rows.append(
        [InlineKeyboardButton(text="✍ Свой / описать", callback_data=CB_DESIGN_FORMAT_CUSTOM)]
    )
    rows.extend(_nav_rows(can_back=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_design_reference_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Пропустить", callback_data=CB_DESIGN_REF_SKIP)]]
    rows.extend(_nav_rows(can_back=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_files_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Готово", callback_data=CB_FILES_DONE)]]
    rows.extend(_nav_rows(can_back=True))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data=CB_SEND)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_BACK)],
            [InlineKeyboardButton(text="✏️ Начать заново", callback_data=CB_EDIT)],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=CB_CANCEL)],
        ]
    )
