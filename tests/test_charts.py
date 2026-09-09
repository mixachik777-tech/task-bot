"""
Smoke-тесты PNG-генератора. Проверяем, что:
- возвращаются bytes с PNG-сигнатурой;
- кириллица в подписях не ломает рендер;
- работа на пустом daily.
"""

from datetime import date, timedelta

from app.bot.utils.charts import render_productivity_chart


def _png_signature(b: bytes) -> bool:
    return b[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_week_with_data():
    daily = [
        (date(2026, 5, 10) + timedelta(days=i), i % 4, (i + 1) % 3)
        for i in range(7)
    ]
    out = render_productivity_chart(
        daily, user_name="Иван Иванов", period_label="Неделя"
    )
    assert _png_signature(out)
    assert len(out) > 1000


def test_render_empty():
    out = render_productivity_chart([], user_name="Anonymous", period_label="Сегодня")
    assert _png_signature(out)


def test_render_month_30_days_does_not_crash():
    daily = [
        (date(2026, 4, 18) + timedelta(days=i), i % 3, i % 5)
        for i in range(30)
    ]
    out = render_productivity_chart(
        daily, user_name="Long Name " * 5, period_label="Месяц"
    )
    assert _png_signature(out)
