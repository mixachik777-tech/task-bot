"""
Глобальный accessor APScheduler. Парный к app/bot/runtime.py.

Используется TaskService / TaskActionsService для добавления и удаления
job'ов напоминаний по созданным/завершённым задачам.

Инициализация в app/main.py: set_scheduler(scheduler) после
AsyncIOScheduler(...) и до scheduler.start().
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

_scheduler: AsyncIOScheduler | None = None


def set_scheduler(scheduler: AsyncIOScheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def get_scheduler() -> AsyncIOScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized — call set_scheduler() first")
    return _scheduler
