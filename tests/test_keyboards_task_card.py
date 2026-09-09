"""Юнит-тесты клавиатуры карточки задачи (inline-кнопки по статусу)."""

from app.bot.keyboards.task_card import (
    build_cancel_confirm_kb,
    build_complete_action_kb,
    build_complete_skip_kb,
    build_question_answer_compose_kb,
    build_question_answer_kb,
    build_question_compose_kb,
    build_rework_finalize_kb,
    build_task_card_kb,
)
from app.db.enums import TaskStatus


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _callbacks(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


def test_task_card_kb_new_has_accept_and_cancel():
    kb = build_task_card_kb(TaskStatus.NEW.value, 42)
    cbs = _callbacks(kb)
    assert any(c == "task_accept:42" for c in cbs)
    assert any(c == "tcancel_ask:42" for c in cbs)


def test_task_card_kb_in_progress_has_reassign_and_question():
    """С 2026-05-29 в карточке in_progress появилась кнопка «🔄 Передать другому»
    рядом с «Завершить»/«Задать вопрос»/«Комментарий»/«Снять»."""
    kb = build_task_card_kb(TaskStatus.IN_PROGRESS.value, 42)
    cbs = _callbacks(kb)
    assert any(c == "task_complete:42" for c in cbs)
    assert any(c == "task_question:42" for c in cbs)
    assert any(c == "task_reassign:42" for c in cbs)
    assert any(c == "task_comment:42" for c in cbs)
    assert any(c == "tcancel_ask:42" for c in cbs)


def test_task_card_kb_awaiting_approval_minimal():
    kb = build_task_card_kb(TaskStatus.AWAITING_APPROVAL.value, 42)
    cbs = _callbacks(kb)
    assert "task_comment:42" in cbs
    assert "tcancel_ask:42" in cbs
    # на согласовании НЕ должно быть «Завершить» — мяч у постановщика
    assert "task_complete:42" not in cbs


def test_task_card_kb_done_and_cancelled_no_buttons():
    assert build_task_card_kb(TaskStatus.DONE.value, 42) is None
    assert build_task_card_kb(TaskStatus.CANCELLED.value, 42) is None


def test_cancel_confirm_kb_has_yes_and_back():
    cbs = _callbacks(build_cancel_confirm_kb(42))
    assert "task_cancel:42" in cbs
    assert "tcancel_back:42" in cbs


def test_complete_action_kb_single_button():
    kb = build_complete_action_kb(42)
    assert _callbacks(kb) == ["task_complete:42"]


def test_complete_skip_kb_has_done_skip_abort():
    cbs = _callbacks(build_complete_skip_kb(42))
    assert "tcomp_done:42" in cbs
    assert "tcomp_skip:42" in cbs
    assert "tcomp_abort:42" in cbs


def test_rework_finalize_kb_has_send_and_abort():
    cbs = _callbacks(build_rework_finalize_kb(42))
    assert "trework_send:42" in cbs
    assert "trework_abort:42" in cbs


def test_question_compose_kb_has_send_and_abort():
    cbs = _callbacks(build_question_compose_kb(42))
    assert "tquest_send:42" in cbs
    assert "tquest_abort:42" in cbs


def test_question_answer_compose_kb_has_send_and_abort():
    cbs = _callbacks(build_question_answer_compose_kb(42))
    assert "tqans_send:42" in cbs
    assert "tqans_abort:42" in cbs


def test_question_answer_kb_carries_history_id():
    cbs = _callbacks(build_question_answer_kb(task_id=42, question_history_id=99))
    assert cbs == ["task_answer:42:99"]
