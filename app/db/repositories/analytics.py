"""
Репозиторий аналитики — агрегаты по задачам для меню «📊 Статистика»
и AI-инструмента get_analytics.

Контракт:
- scope ∈ {"user", "department", "all"}; user/department требуют соотв. id.
- period ∈ {"today", "week", "month"}; границы вычисляются по локальной
  таймзоне (Europe/Moscow), но фильтруются по UTC-полям модели.
- Все методы НЕ коммитят сессию.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.enums import HistoryEventType, TaskStatus
from app.db.models import Task, TaskHistory

PERIODS: tuple[str, ...] = ("today", "week", "month")
SCOPES: tuple[str, ...] = ("user", "department", "all")


@dataclass(frozen=True)
class AnalyticsSummary:
    """Сводка для отображения. avg_completion_seconds=None если задач нет."""

    created: int
    completed: int
    in_progress: int
    overdue: int
    avg_completion_seconds: float | None
    period_from_utc: datetime
    period_to_utc: datetime


@dataclass(frozen=True)
class UserProductivity:
    """
    Метрики продуктивности конкретного исполнителя за период.

    «Создал» здесь намеренно не учитывается — в task-bot задачи ставит только
    руководитель, рядовые сотрудники только исполняют (см. memory project
    `task_creation_role`).

    Метрика «время реакции» (created→accepted) сюда сознательно не включена:
    до момента accept задача висит без исполнителя в общем топике и любой
    из отдела может её взять — это не зависит от конкретного юзера.

    `cancelled` берётся из task_history (event=cancelled в окне) для задач,
    где юзер был assignee. При cancel поле completed_at не выставляется, поэтому
    статус+completed_at в окне как для done тут не работает.
    """

    assigned: int  # accepted_at юзера в период
    completed: int  # status=done И completed_at юзера в период
    cancelled: int  # event=cancelled в task_history за период по задачам юзера
    on_time_count: int  # из completed в окне: completed_at <= deadline
    in_progress_now: int  # текущий снимок: status=in_progress AND assignee=user
    overdue_now: int  # текущий снимок: status in (new,in_progress) AND deadline < now
    completion_rate_pct: float | None  # completed / assigned * 100, None если assigned == 0
    on_time_rate_pct: float | None  # on_time_count / completed * 100, None если completed == 0
    avg_completion_seconds: float | None
    daily_breakdown: list[tuple[date, int, int]]  # (день, assigned, completed)
    period_from_utc: datetime
    period_to_utc: datetime


@dataclass(frozen=True)
class ActiveTasksBuckets:
    """
    Активные задачи исполнителя (assignee = user, status in new/in_progress),
    сгруппированные лесенкой по «температуре дедлайна». Каждая задача
    попадает ровно в одну корзину — первая подходящая сверху вниз:

      overdue       deadline < now
      burning_24h   0 <= deadline-now < 24ч
      today         дедлайн сегодня по локальной дате (редкий остаток после burning)
      this_week     1d <= deadline-now < 7d
      later         deadline-now >= 7d
    """

    overdue: list[Task] = field(default_factory=list)
    burning_24h: list[Task] = field(default_factory=list)
    today: list[Task] = field(default_factory=list)
    this_week: list[Task] = field(default_factory=list)
    later: list[Task] = field(default_factory=list)
    now_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total(self) -> int:
        return (
            len(self.overdue)
            + len(self.burning_24h)
            + len(self.today)
            + len(self.this_week)
            + len(self.later)
        )


def period_bounds_utc(period: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    Вычисляет [from, to) для period в UTC.

    today: от начала текущих суток в локальном TZ до текущего now (UTC).
    week:  rolling 7 дней (now - 7d, now).
    month: rolling 30 дней (now - 30d, now).

    Rolling window для week/month проще и предсказуемее календарного:
    не зависит от того, в какой день недели/месяца спросили.
    """
    if period not in PERIODS:
        raise ValueError(f"unknown period: {period}")
    now_utc = now if now is not None else datetime.now(timezone.utc)
    if period == "today":
        local_tz = ZoneInfo(settings.TZ)
        local_now = now_utc.astimezone(local_tz)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_start.astimezone(timezone.utc), now_utc
    if period == "week":
        return now_utc - timedelta(days=7), now_utc
    return now_utc - timedelta(days=30), now_utc


def _scope_predicate(
    *,
    scope: str,
    user_id: int | None,
    department_id: int | None,
) -> Any:
    """
    Возвращает SQL-предикат для фильтрации задач по scope.

    user: задача либо создана юзером, либо назначена на него.
    department: задача в отделе.
    all: без фильтра (True).
    """
    if scope == "user":
        if user_id is None:
            raise ValueError("scope=user requires user_id")
        return or_(Task.creator_id == user_id, Task.assignee_id == user_id)
    if scope == "department":
        if department_id is None:
            raise ValueError("scope=department requires department_id")
        return Task.department_id == department_id
    if scope == "all":
        return Task.id.is_not(None)
    raise ValueError(f"unknown scope: {scope}")


class AnalyticsRepository:
    @staticmethod
    async def get_summary(
        session: AsyncSession,
        *,
        scope: str,
        period: str,
        user_id: int | None = None,
        department_id: int | None = None,
        now: datetime | None = None,
    ) -> AnalyticsSummary:
        now_utc = now if now is not None else datetime.now(timezone.utc)
        period_from, period_to = period_bounds_utc(period, now=now_utc)
        scope_pred = _scope_predicate(
            scope=scope, user_id=user_id, department_id=department_id
        )

        created_q = (
            select(func.count())
            .select_from(Task)
            .where(scope_pred, Task.created_at >= period_from, Task.created_at < period_to)
        )
        completed_q = (
            select(func.count())
            .select_from(Task)
            .where(
                scope_pred,
                Task.status == TaskStatus.DONE.value,
                Task.completed_at >= period_from,
                Task.completed_at < period_to,
            )
        )
        in_progress_q = (
            select(func.count())
            .select_from(Task)
            .where(scope_pred, Task.status == TaskStatus.IN_PROGRESS.value)
        )
        overdue_q = (
            select(func.count())
            .select_from(Task)
            .where(
                scope_pred,
                Task.status.in_(
                    [TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]
                ),
                Task.deadline < now_utc,
            )
        )
        avg_q = (
            select(
                func.avg(
                    func.extract("epoch", Task.completed_at - Task.created_at)
                )
            )
            .where(
                scope_pred,
                Task.status == TaskStatus.DONE.value,
                Task.completed_at >= period_from,
                Task.completed_at < period_to,
            )
        )

        created = int((await session.execute(created_q)).scalar_one())
        completed = int((await session.execute(completed_q)).scalar_one())
        in_progress = int((await session.execute(in_progress_q)).scalar_one())
        overdue = int((await session.execute(overdue_q)).scalar_one())
        avg_seconds_raw = (await session.execute(avg_q)).scalar_one_or_none()
        avg_seconds = float(avg_seconds_raw) if avg_seconds_raw is not None else None

        return AnalyticsSummary(
            created=created,
            completed=completed,
            in_progress=in_progress,
            overdue=overdue,
            avg_completion_seconds=avg_seconds,
            period_from_utc=period_from,
            period_to_utc=period_to,
        )

    @staticmethod
    async def get_user_productivity(
        session: AsyncSession,
        *,
        user_id: int,
        period: str,
        now: datetime | None = None,
    ) -> UserProductivity:
        """
        Продуктивность ИСПОЛНИТЕЛЯ. Все агрегаты по `assignee_id = user_id`.

        Daily breakdown — два запроса с date_trunc по локальной TZ; на стороне
        Python склеиваются в плотный массив дней без пропусков (для красивого
        графика без «провалов»).
        """
        now_utc = now if now is not None else datetime.now(timezone.utc)
        period_from, period_to = period_bounds_utc(period, now=now_utc)
        local_tz = ZoneInfo(settings.TZ)

        # assigned за период: accepted_at в окне
        assigned_q = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.accepted_at.is_not(None),
                Task.accepted_at >= period_from,
                Task.accepted_at < period_to,
            )
        )
        completed_q = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.DONE.value,
                Task.completed_at >= period_from,
                Task.completed_at < period_to,
            )
        )
        in_progress_q = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.IN_PROGRESS.value,
            )
        )
        overdue_q = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status.in_(
                    [TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]
                ),
                Task.deadline < now_utc,
            )
        )
        avg_q = (
            select(
                func.avg(
                    func.extract("epoch", Task.completed_at - Task.created_at)
                )
            )
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.DONE.value,
                Task.completed_at >= period_from,
                Task.completed_at < period_to,
            )
        )

        # daily breakdown: date_trunc по локальной TZ, чтобы день начинался
        # в 00:00 MSK, а не 00:00 UTC. AT TIME ZONE 'Europe/Moscow' конвертит
        # TIMESTAMPTZ в наивный локальный timestamp, дальше CAST в date.
        tz_name = settings.TZ
        day_expr_assigned = cast(
            func.date_trunc(
                "day", func.timezone(tz_name, Task.accepted_at)
            ),
            Date,
        ).label("day")
        daily_assigned_q = (
            select(day_expr_assigned, func.count().label("cnt"))
            .where(
                Task.assignee_id == user_id,
                Task.accepted_at.is_not(None),
                Task.accepted_at >= period_from,
                Task.accepted_at < period_to,
            )
            .group_by(day_expr_assigned)
        )
        day_expr_completed = cast(
            func.date_trunc(
                "day", func.timezone(tz_name, Task.completed_at)
            ),
            Date,
        ).label("day")
        daily_completed_q = (
            select(day_expr_completed, func.count().label("cnt"))
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.DONE.value,
                Task.completed_at >= period_from,
                Task.completed_at < period_to,
            )
            .group_by(day_expr_completed)
        )

        # on_time: из completed в окне — те, что закрыли до дедлайна
        on_time_q = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.DONE.value,
                Task.completed_at >= period_from,
                Task.completed_at < period_to,
                Task.completed_at <= Task.deadline,
            )
        )
        # cancelled: через task_history, т.к. при cancel completed_at не ставится.
        # DISTINCT task_id на случай нескольких cancel-событий по одной задаче
        # (теоретически возможны после reassign-сценариев).
        cancelled_q = (
            select(func.count(func.distinct(TaskHistory.task_id)))
            .select_from(TaskHistory)
            .join(Task, Task.id == TaskHistory.task_id)
            .where(
                Task.assignee_id == user_id,
                TaskHistory.event_type == HistoryEventType.CANCELLED.value,
                TaskHistory.created_at >= period_from,
                TaskHistory.created_at < period_to,
            )
        )

        assigned = int((await session.execute(assigned_q)).scalar_one())
        completed = int((await session.execute(completed_q)).scalar_one())
        in_progress = int((await session.execute(in_progress_q)).scalar_one())
        overdue = int((await session.execute(overdue_q)).scalar_one())
        on_time = int((await session.execute(on_time_q)).scalar_one())
        cancelled = int((await session.execute(cancelled_q)).scalar_one())
        avg_raw = (await session.execute(avg_q)).scalar_one_or_none()
        avg_seconds = float(avg_raw) if avg_raw is not None else None

        completion_rate = (
            round(100.0 * completed / assigned, 1) if assigned > 0 else None
        )
        on_time_rate = (
            round(100.0 * on_time / completed, 1) if completed > 0 else None
        )

        assigned_rows = (await session.execute(daily_assigned_q)).all()
        completed_rows = (await session.execute(daily_completed_q)).all()
        assigned_map: dict[date, int] = {row.day: int(row.cnt) for row in assigned_rows}
        completed_map: dict[date, int] = {row.day: int(row.cnt) for row in completed_rows}

        # плотный список дней по локальному календарю периода
        local_from = period_from.astimezone(local_tz).date()
        local_to = period_to.astimezone(local_tz).date()
        # упор в как минимум 1 день
        days: list[date] = []
        cur = local_from
        while cur <= local_to:
            days.append(cur)
            cur = cur + timedelta(days=1)
        daily_breakdown = [
            (d, assigned_map.get(d, 0), completed_map.get(d, 0)) for d in days
        ]

        return UserProductivity(
            assigned=assigned,
            completed=completed,
            cancelled=cancelled,
            on_time_count=on_time,
            in_progress_now=in_progress,
            overdue_now=overdue,
            completion_rate_pct=completion_rate,
            on_time_rate_pct=on_time_rate,
            avg_completion_seconds=avg_seconds,
            daily_breakdown=daily_breakdown,
            period_from_utc=period_from,
            period_to_utc=period_to,
        )

    @staticmethod
    async def get_user_active_tasks(
        session: AsyncSession,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> ActiveTasksBuckets:
        """
        Активные задачи исполнителя, разложенные по «температуре дедлайна».

        Источник: tasks где assignee_id = user AND status IN (new, in_progress).
        Сортировка внутри корзин — по deadline ASC (самые срочные сверху).

        Лесенка определена так, что каждая задача попадает в ровно одну корзину
        (см. ActiveTasksBuckets docstring).
        """
        now_utc = now if now is not None else datetime.now(timezone.utc)
        local_tz = ZoneInfo(settings.TZ)
        local_today = now_utc.astimezone(local_tz).date()

        stmt = (
            select(Task)
            .options(selectinload(Task.department))
            .where(
                Task.assignee_id == user_id,
                Task.status.in_(
                    [TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]
                ),
            )
            .order_by(Task.deadline.asc(), Task.id.asc())
        )
        result = await session.execute(stmt)
        tasks = list(result.scalars().all())

        buckets = ActiveTasksBuckets(now_utc=now_utc)
        day_24h = timedelta(hours=24)
        week_7d = timedelta(days=7)

        for t in tasks:
            delta = t.deadline - now_utc
            if delta < timedelta(0):
                buckets.overdue.append(t)
                continue
            if delta < day_24h:
                buckets.burning_24h.append(t)
                continue
            local_deadline = t.deadline.astimezone(local_tz).date()
            if local_deadline == local_today:
                # очень редкий остаток: дедлайн сегодня, но > 24ч от now
                # (возможен только если сейчас 00:xx, а дедлайн 23:xx того же дня)
                buckets.today.append(t)
                continue
            if delta < week_7d:
                buckets.this_week.append(t)
                continue
            buckets.later.append(t)

        return buckets

    @staticmethod
    async def get_user_stale_tasks(
        session: AsyncSession,
        *,
        user_id: int,
        stale_days: int = 7,
        now: datetime | None = None,
    ) -> list[Task]:
        """
        Залипшие задачи исполнителя: status=in_progress, последнее событие
        в task_history было более `stale_days` дней назад (или истории нет —
        тогда смотрим accepted_at).

        Используется коррелированным подзапросом MAX(created_at) по task_history
        конкретной задачи. На малом объёме нормально; если задач станет много —
        переписать через window function.
        """
        now_utc = now if now is not None else datetime.now(timezone.utc)
        threshold = now_utc - timedelta(days=stale_days)

        last_event_subq = (
            select(func.max(TaskHistory.created_at))
            .where(TaskHistory.task_id == Task.id)
            .correlate(Task)
            .scalar_subquery()
        )

        stmt = (
            select(Task)
            .options(selectinload(Task.department))
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.IN_PROGRESS.value,
                func.coalesce(last_event_subq, Task.accepted_at) < threshold,
            )
            .order_by(Task.deadline.asc(), Task.id.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
