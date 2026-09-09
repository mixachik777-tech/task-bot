"""
Юнит-тесты клавиатур FSM создания задачи. Каждый шаг должен иметь
кнопку «❌ Отменить»; промежуточные — ещё и «⬅️ Назад».
"""

from app.bot.keyboards.create_task import (
    CB_BACK,
    CB_CANCEL,
    CB_DESC_SKIP,
    CB_DESIGN_REF_SKIP,
    CB_FILES_DONE,
    build_dept_kb,
    build_description_kb,
    build_design_reference_kb,
    build_files_kb,
    build_preview_kb,
    build_priority_kb,
    build_text_step_nav_kb,
)
from app.db.models import Department


def _all_callback_data(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]


def _dept(id_: int, code: str, name: str) -> Department:
    return Department(id=id_, code=code, name=name, topic_id=0, is_active=True)


def test_dept_kb_has_cancel_no_back():
    kb = build_dept_kb([_dept(1, "designers", "Дизайнеры")])
    cbs = _all_callback_data(kb)
    assert CB_CANCEL in cbs
    assert CB_BACK not in cbs  # первый шаг — назад некуда


def test_priority_kb_has_back_and_cancel():
    cbs = _all_callback_data(build_priority_kb())
    assert CB_BACK in cbs
    assert CB_CANCEL in cbs


def test_text_step_nav_kb_has_back_and_cancel():
    cbs = _all_callback_data(build_text_step_nav_kb())
    assert cbs == [CB_BACK, CB_CANCEL]


def test_description_kb_has_skip_back_cancel():
    cbs = _all_callback_data(build_description_kb())
    assert CB_DESC_SKIP in cbs
    assert CB_BACK in cbs
    assert CB_CANCEL in cbs


def test_design_reference_kb_has_skip_back_cancel():
    cbs = _all_callback_data(build_design_reference_kb())
    assert CB_DESIGN_REF_SKIP in cbs
    assert CB_BACK in cbs
    assert CB_CANCEL in cbs


def test_files_kb_has_done_back_cancel():
    cbs = _all_callback_data(build_files_kb())
    assert CB_FILES_DONE in cbs
    assert CB_BACK in cbs
    assert CB_CANCEL in cbs


def test_preview_kb_has_back_and_send():
    cbs = _all_callback_data(build_preview_kb())
    assert CB_BACK in cbs
    assert CB_CANCEL in cbs
    assert "ct_send" in cbs
    assert "ct_edit" in cbs


def test_description_skip_confirm_kb_has_both_options():
    from app.bot.keyboards.create_task import (
        CB_DESC_SKIP_BACK,
        CB_DESC_SKIP_CONFIRM,
        build_description_skip_confirm_kb,
    )
    kb = build_description_skip_confirm_kb()
    cbs = _all_callback_data(kb)
    assert CB_DESC_SKIP_CONFIRM in cbs
    assert CB_DESC_SKIP_BACK in cbs


def test_description_kb_button_is_skip_descr_explicit():
    """После 2026-05-29 кнопка переименована со «Пропустить» на
    «🚫 Описания не требуется» — чтобы пропуск был осознанным."""
    kb = build_description_kb()
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Описания не требуется" in t for t in texts), texts
