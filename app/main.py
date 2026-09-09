import asyncio
import sys
from datetime import timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from redis.asyncio import Redis

from app.bot import heartbeat
from app.bot.handlers.admin import router as admin_router
from app.bot.handlers.admin_workload import router as admin_workload_router
from app.bot.handlers.ai_chat import router as ai_chat_router
from app.bot.handlers.all_tasks_admin import router as all_tasks_admin_router
from app.bot.handlers.analytics import router as analytics_router
from app.bot.handlers.archive import router as archive_router
from app.bot.handlers.cabinet import router as cabinet_router
from app.bot.handlers.team import router as team_router
from app.bot.handlers.create_task import router as create_task_router
from app.bot.handlers.menu_stubs import router as menu_stubs_router
from app.bot.handlers.onboarding import router as onboarding_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.task_actions import router as task_actions_router
from app.bot.handlers.task_question import router as task_question_router
from app.bot.handlers.task_reassign import router as task_reassign_router
from app.bot.menu_refresh import MenuAwareBot
from app.bot.middlewares.album import AlbumMiddleware
from app.bot.middlewares.auth import AuthMiddleware
from app.bot.middlewares.heartbeat import HeartbeatMiddleware
from app.bot.runtime import set_bot, set_redis as set_runtime_redis
from app.config import settings
from app.scheduler.bootstrap import bootstrap_scheduler
from app.scheduler.notifications import set_redis as set_notifications_redis
from app.scheduler.runtime import set_scheduler

POLLING_BACKOFF_S = 5
WATCHDOG_INTERVAL_S = 60
WATCHDOG_GETME_TIMEOUT_S = 10
# Сколько ждём, пока polling завершится после dp.stop_polling().
# stop_polling выставляет event, aiogram внутри start_polling видит его
# через asyncio.wait(), затем сам отменяет внутренние таски _polling и
# чисто завершает start_polling. Это в отличие от прямого
# polling_task.cancel(), который оставляет _polling-таски орфанами,
# каждый продолжает свой собственный retry-loop на TelegramConflictError
# (root cause инцидента 2026-05-18: накопление зомби-polling за дни).
POLLING_STOP_TIMEOUT_S = 20


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        enqueue=False,
        backtrace=True,
        diagnose=False,
    )


async def _watchdog(
    bot: Bot,
    redis: Redis,
    dp: Dispatcher,
    polling_task: asyncio.Task,
    generation: int,
) -> None:
    """
    Раз в минуту дёргает getMe. При сбое/зависании вызывает dp.stop_polling() —
    aiogram внутри start_polling видит выставленный stop-event и сам корректно
    отменяет свои внутренние _polling-таски, после чего start_polling
    завершается. Supervisor подхватывает завершение и стартует следующее
    поколение.

    Ранее watchdog делал polling_task.cancel() напрямую — это оставляло
    внутренние _polling-таски aiogram'а живыми (asyncio.wait() внутри
    start_polling не отменяет pending tasks при CancelledError снаружи).
    Каждое поколение порождало зомби, дерущегося за getUpdates. См. инцидент
    2026-05-25 (9 зомби за 3 дня).
    """
    while not polling_task.done():
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            await asyncio.wait_for(bot.get_me(), timeout=WATCHDOG_GETME_TIMEOUT_S)
            await heartbeat.bump_getme(redis)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "watchdog[gen{}]: getMe failed/hung ({}), stopping polling gracefully",
                generation,
                exc,
            )
            try:
                await asyncio.wait_for(dp.stop_polling(), timeout=POLLING_STOP_TIMEOUT_S)
            except (asyncio.TimeoutError, RuntimeError, Exception) as exc2:  # noqa: BLE001
                logger.error(
                    "watchdog[gen{}]: stop_polling failed ({}), forcing task.cancel as last resort",
                    generation,
                    exc2,
                )
                polling_task.cancel()
            return


async def _run_polling_supervised(dp: Dispatcher, bot: Bot, redis: Redis) -> None:
    """
    Бесконечный retry-loop вокруг dp.start_polling.
    На каждой итерации параллельно запускается watchdog. Если watchdog видит
    зависание/сбой — он зовёт dp.stop_polling(), aiogram внутри корректно
    отменяет свои _polling-таски, start_polling завершается, мы стартуем
    новое поколение.

    Контракт: НИКОГДА не запускать новую итерацию, пока предыдущий
    polling_task не завершился. Раньше для гарантии этого делался
    polling_task.cancel() в finally — это и было корнем накопления зомби:
    asyncio.wait() внутри aiogram.start_polling не отменяет pending tasks при
    CancelledError снаружи, внутренний _polling продолжал retry-loop
    независимо. Теперь сначала всегда dp.stop_polling() (корректный путь),
    polling_task.cancel() остаётся только как last-resort при таймауте.
    """
    generation = 0
    while True:
        generation += 1
        polling_task = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
            name=f"polling-gen{generation}",
        )
        watchdog_task = asyncio.create_task(
            _watchdog(bot, redis, dp, polling_task, generation),
            name=f"watchdog-gen{generation}",
        )
        logger.info("polling supervised: gen{} started", generation)
        try:
            await polling_task
            logger.warning(
                "polling[gen{}] returned without exception — restarting",
                generation,
            )
        except asyncio.CancelledError:
            logger.warning(
                "polling[gen{}] cancelled — restarting",
                generation,
            )
        except Exception:
            logger.exception("polling[gen{}] crashed — restarting", generation)
        finally:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            # Гарантируем, что polling-task действительно завершён, иначе
            # следующая итерация поднимет ВТОРОЙ getUpdates и Telegram
            # начнёт раздавать апдейты случайно между поколениями.
            # Корректный путь — stop_polling (aiogram сам прибирает внутренние
            # таски), cancel — только last-resort.
            if not polling_task.done():
                try:
                    await asyncio.wait_for(dp.stop_polling(), timeout=POLLING_STOP_TIMEOUT_S)
                except (asyncio.TimeoutError, RuntimeError, Exception) as exc:  # noqa: BLE001
                    logger.error(
                        "supervisor[gen{}]: stop_polling failed ({}), forcing task.cancel",
                        generation,
                        exc,
                    )
                    polling_task.cancel()
            try:
                await asyncio.wait_for(polling_task, timeout=POLLING_STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.critical(
                    "polling[gen{}] не завершился за {}s после stop_polling+cancel — "
                    "ВОЗМОЖНО ЗОМБИ-ТАСК! Следующее поколение может конкурировать "
                    "за getUpdates. Если конфликт повторится — нужен рестарт контейнера.",
                    generation,
                    POLLING_STOP_TIMEOUT_S,
                )
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await asyncio.sleep(POLLING_BACKOFF_S)


async def _setup_bot_commands(bot: Bot) -> None:
    """Регистрирует команды в нативном меню Telegram.

    Личка (BotCommandScopeAllPrivateChats) — пусто: все действия идут через
    reply/inline-кнопки. Так Telegram не показывает «синюю кнопку» меню рядом
    с полем ввода — она была бы пустой и сбивала бы юзеров.

    Супергруппа, только админам чата (BotCommandScopeAllChatAdministrators) —
    показываем три setup-команды: они контекстно-зависимые (нужны chat_id /
    thread_id текущего чата/топика) и переделать их на кнопки нельзя.
    Появляются у админов чата как клик-пункты в Telegram-меню — печатать
    руками не нужно.
    """
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import (
        BotCommand,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeAllPrivateChats,
    )

    admin_chat_cmds = [
        BotCommand(
            command="set_chat",
            description="Сохранить chat_id этой супергруппы",
        ),
        BotCommand(
            command="set_topic",
            description="Привязать текущий топик к отделу: /set_topic <dept_code>",
        ),
        BotCommand(
            command="set_leadership_topic",
            description="Назначить текущий топик «Руководство»",
        ),
    ]
    try:
        await bot.set_my_commands(
            commands=[],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(
            commands=admin_chat_cmds,
            scope=BotCommandScopeAllChatAdministrators(),
        )
        logger.info("bot commands menu: registered (private=пусто, admin-chat=3)")
    except TelegramBadRequest as exc:
        logger.warning("bot commands menu: setup failed: {}", exc)


async def main() -> None:
    setup_logging()
    logger.info("Старт task-bot")

    redis = Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB)
    storage = RedisStorage(redis=redis)

    bot = MenuAwareBot(
        token=settings.BOT_TOKEN,
        redis=redis,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    set_bot(bot)
    set_runtime_redis(redis)
    set_notifications_redis(redis)

    dp = Dispatcher(storage=storage)

    @dp.errors()
    async def _on_error(event):  # type: ignore[no-untyped-def]
        logger.exception(
            "handler error update={} exception={}",
            getattr(event, "update", None),
            getattr(event, "exception", None),
        )
        return True

    heartbeat_mw = HeartbeatMiddleware(redis)
    auth_mw = AuthMiddleware()
    album_mw = AlbumMiddleware()
    dp.message.middleware(heartbeat_mw)
    dp.callback_query.middleware(heartbeat_mw)
    dp.message.middleware(auth_mw)
    dp.callback_query.middleware(auth_mw)
    dp.message.middleware(album_mw)

    dp.include_router(admin_router)
    dp.include_router(onboarding_router)
    dp.include_router(start_router)
    dp.include_router(create_task_router)
    dp.include_router(task_actions_router)
    dp.include_router(task_question_router)
    dp.include_router(task_reassign_router)
    dp.include_router(archive_router)
    dp.include_router(team_router)
    dp.include_router(analytics_router)
    dp.include_router(cabinet_router)
    dp.include_router(all_tasks_admin_router)
    dp.include_router(admin_workload_router)
    dp.include_router(ai_chat_router)
    dp.include_router(menu_stubs_router)

    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    set_scheduler(scheduler)
    await bootstrap_scheduler(scheduler)
    scheduler.start()
    logger.info("APScheduler стартовал")

    await heartbeat.bump_getme(redis)

    try:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("delete_webhook ok (self-conflict защита перед polling)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_webhook failed: {}", exc)
        await _setup_bot_commands(bot)
        logger.info("Запуск polling (supervised)")
        await _run_polling_supervised(dp, bot, redis)
    finally:
        scheduler.shutdown(wait=True)
        await bot.session.close()
        await redis.aclose()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")
