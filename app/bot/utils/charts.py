"""
Серверный рендер PNG-графиков через matplotlib (Agg backend, без GUI).

Контракт:
- render_productivity_chart(daily, user_name, period_label) -> bytes
  Возвращает PNG-байты для отправки через aiogram BufferedInputFile.
- Шрифт DejaVu Sans (поставляется с matplotlib, умеет кириллицу).
"""

from __future__ import annotations

import io
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# DejaVu Sans — дефолт matplotlib, кириллицу читает. Явно фиксируем,
# чтобы случайный «помощник» не подменил на шрифт без cyrillic-glyph'ов.
plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

COLOR_ASSIGNED = "#3b82f6"  # blue-500
COLOR_COMPLETED = "#22c55e"  # green-500
COLOR_GRID = "#e5e7eb"


def _xtick_step(n_days: int) -> int:
    """Шаг показа дат по X: чтобы не превращалось в кашу из 30 меток."""
    if n_days <= 14:
        return 1
    if n_days <= 21:
        return 2
    return 3


def render_productivity_chart(
    daily: list[tuple[date, int, int]],
    *,
    user_name: str,
    period_label: str,
) -> bytes:
    """
    Группированный бар-чарт: для каждого дня периода — две колонки
    «назначено / выполнено». Если daily пустой — пустая канва с подписью.
    """
    fig, ax = plt.subplots(figsize=(8.0, 4.0), dpi=130)

    if not daily:
        ax.text(
            0.5,
            0.5,
            "Нет данных за выбранный период",
            ha="center",
            va="center",
            fontsize=12,
            color="#6b7280",
            transform=ax.transAxes,
        )
        ax.set_axis_off()
        ax.set_title(
            f"{user_name} · {period_label}", fontsize=12, color="#111827"
        )
        return _finalize(fig)

    n = len(daily)
    x = list(range(n))
    width = 0.38
    assigned = [a for _, a, _ in daily]
    completed = [c for _, _, c in daily]

    ax.bar(
        [xi - width / 2 for xi in x],
        assigned,
        width,
        label="Назначено",
        color=COLOR_ASSIGNED,
    )
    ax.bar(
        [xi + width / 2 for xi in x],
        completed,
        width,
        label="Выполнено",
        color=COLOR_COMPLETED,
    )

    step = _xtick_step(n)
    ax.set_xticks(x[::step])
    ax.set_xticklabels(
        [d.strftime("%d.%m") for d, _, _ in daily][::step],
        rotation=45,
        ha="right",
    )
    max_val = max([0, *assigned, *completed])
    ax.set_ylim(0, max(max_val + 1, 2))
    # целые числа на оси Y (без 0.5 / 1.5)
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax.grid(axis="y", color=COLOR_GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.set_title(f"{user_name} · {period_label}", fontsize=13, color="#111827")
    ax.legend(loc="upper right", frameon=False)

    return _finalize(fig)


def _finalize(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
