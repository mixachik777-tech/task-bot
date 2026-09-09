"""Инлайн-клавиатуры для карточки задачи и FSM действий."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.enums import TaskStatus


def build_task_card_kb(status: str, task_id: int) -> InlineKeyboardMarkup | None:
    """
    Клавиатура карточки в топике отдела. В зеркале «Руководства» НЕ используется
    (там reply_markup=None, см. DECISIONS.md → Этап 4 / Правка 2).

    Permission-фильтр (creator/assignee/admin) — на стороне callback-хэндлера,
    не в кнопке. Кнопки видны всем, но клик отдаст alert «недостаточно прав».
    """
    if status == TaskStatus.NEW.value:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✋ Принять в работу",
                        callback_data=f"task_accept:{task_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Снять задачу",
                        callback_data=f"tcancel_ask:{task_id}",
                    )
                ],
            ]
        )
    if status == TaskStatus.IN_PROGRESS.value:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Завершить",
                        callback_data=f"task_complete:{task_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❓ Задать вопрос",
                        callback_data=f"task_question:{task_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Передать другому",
                        callback_data=f"task_reassign:{task_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Комментарий",
                        callback_data=f"task_comment:{task_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Снять задачу",
                        callback_data=f"tcancel_ask:{task_id}",
                    )
                ],
            ]
        )
    if status == TaskStatus.AWAITING_APPROVAL.value:
        # На согласовании постановщик ждёт отчёта; его approval-kb
        # приклеена к DM-уведомлению. Здесь оставляем «Комментарий» и
        # «Снять задачу» — на случай, если задача стала неактуальной
        # уже после отправки на согласование.
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Комментарий",
                        callback_data=f"task_comment:{task_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Снять задачу",
                        callback_data=f"tcancel_ask:{task_id}",
                    )
                ],
            ]
        )
    return None


def build_cancel_confirm_kb(task_id: int) -> InlineKeyboardMarkup:
    """Двухшаговая отмена: после нажатия «❌ Снять задачу» юзер должен
    подтвердить решение. Иначе случайное касание удаляет задачу из
    активного потока (она переходит в архив со статусом cancelled).

    «✅ Да, снять» — реальная отмена (callback task_cancel).
    «⬅️ Назад» — вернуть клавиатуру действий (callback tcancel_back).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, снять",
                    callback_data=f"task_cancel:{task_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"tcancel_back:{task_id}",
                )
            ],
        ]
    )


def build_complete_action_kb(task_id: int) -> InlineKeyboardMarkup:
    """
    Одна кнопка «✅ Завершить» — для DM-уведомлений исполнителю.

    Симметрична build_approval_kb (которая на DM постановщику). Нужна
    при возврате задачи на доработку: исполнитель в личке получает
    уведомление с правками и тут же видит кнопку для следующего шага,
    не открывая карточку в топике.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Завершить",
                    callback_data=f"task_complete:{task_id}",
                )
            ]
        ]
    )


def build_approval_kb(task_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура для постановщика, прикрепляется к уведомлению о завершении
    задачи исполнителем (Этап Б «Согласование»).

    «✅ Согласовано» — закрыть задачу (статус → done).
    «✏️ На доработку» — вернуть исполнителю (статус → in_progress).
      В Этапе В при клике запускается FSM с обязательным комментарием.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Согласовано",
                    callback_data=f"task_approve:{task_id}",
                ),
                InlineKeyboardButton(
                    text="✏️ На доработку",
                    callback_data=f"task_rework:{task_id}",
                ),
            ]
        ]
    )


def build_rework_finalize_kb(task_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура шага «На доработку» (Этап В).

    «✅ Отправить на доработку» — собрать текст+файлы из FSM и отправить
    исполнителю. Кнопка работает только если в state есть непустой comment;
    иначе хендлер показывает alert «нужен текст с правками».
    «❌ Отмена» — выйти из FSM, задача остаётся «На согласовании».
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить на доработку",
                    callback_data=f"trework_send:{task_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"trework_abort:{task_id}",
                )
            ],
        ]
    )


def build_question_compose_kb(task_id: int) -> InlineKeyboardMarkup:
    """Клавиатура шага «Задать вопрос» для исполнителя.

    Юзер шлёт текст вопроса (опционально + один файл/фото) — они копятся
    в FSM TaskQuestion. По «✉️ Отправить» вопрос летит постановщику в DM
    с inline-кнопкой «✏️ Ответить».
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Отправить вопрос",
                    callback_data=f"tquest_send:{task_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"tquest_abort:{task_id}",
                )
            ],
        ]
    )


def build_question_answer_compose_kb(task_id: int) -> InlineKeyboardMarkup:
    """Клавиатура шага «Ответить на вопрос» для постановщика.

    Юзер шлёт текст ответа (опционально + один файл/фото). По
    «✉️ Отправить» ответ улетает исполнителю в DM.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Отправить ответ",
                    callback_data=f"tqans_send:{task_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"tqans_abort:{task_id}",
                )
            ],
        ]
    )


def build_question_answer_kb(task_id: int, question_history_id: int) -> InlineKeyboardMarkup:
    """Кнопка «✏️ Ответить» — крепится к DM-уведомлению постановщику.

    callback_data: task_answer:{task_id}:{question_history_id} — history_id
    нужен, чтобы при логировании ответа связать его с конкретным вопросом
    (одна задача может иметь несколько вопросов).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Ответить",
                    callback_data=f"task_answer:{task_id}:{question_history_id}",
                )
            ]
        ]
    )


def build_complete_skip_kb(task_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура шага «Завершить»: накопитель отчёта.

    Юзер шлёт сообщения (текст и/или файлы) — они копятся в FSM.
    Когда готов — «✅ Готово» отправляет всё постановщику в личку и
    меняет статус задачи на DONE.
    «Без отчёта» — завершить сразу, ничего не прикладывая (старое поведение).
    «❌ Отмена» — выйти из FSM, статус задачи не меняется.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Готово, завершить",
                    callback_data=f"tcomp_done:{task_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Без отчёта",
                    callback_data=f"tcomp_skip:{task_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"tcomp_abort:{task_id}",
                ),
            ],
        ]
    )
