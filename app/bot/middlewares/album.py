"""
AlbumMiddleware — собирает media_group (альбом) в одно срабатывание handler'а.

Корень: при отправке альбома Telegram присылает N отдельных Message с общим
media_group_id за миллисекунды. aiogram диспатчит их параллельно. Если
handler делает read-modify-write FSM (`get_data` → append → `update_data`),
все N задач читают одно состояние, по очереди пишут — последний выигрывает,
остальные файлы теряются. Инциденты «из 2 фото фиксируется 1» — оттуда.

Контракт: первый message из альбома доходит до handler'а через delay, в
data["album"] лежит весь список Message, отсортированный по message_id.
Остальные message из альбома middleware молча проглатывает — handler не
видит их. Single message (без media_group_id) проходит без изменений,
data["album"] отсутствует.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

# Сколько ждём остальных сообщений альбома. Telegram обычно укладывается
# в 100-300мс; 700мс с запасом, чтобы не отрезать хвост на тормозящей сети.
ALBUM_DELAY_S = 0.7


class AlbumMiddleware(BaseMiddleware):
    def __init__(self, delay: float = ALBUM_DELAY_S) -> None:
        self.delay = delay
        # media_group_id → {"messages": [...]}
        self._cache: dict[str, dict[str, Any]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Duck-typing на media_group_id вместо isinstance(Message) — middleware
        # привязан к dp.message, но aiogram теоретически может пробрасывать
        # обёртки; isinstance ломал и тесты с fake-объектами.
        mgid = getattr(event, "media_group_id", None)
        if not mgid:
            return await handler(event, data)
        entry = self._cache.get(mgid)
        if entry is None:
            entry = {"messages": [event]}
            self._cache[mgid] = entry
            try:
                await asyncio.sleep(self.delay)
            finally:
                cached = self._cache.pop(mgid, None)
            if not cached:
                return
            msgs = cached["messages"]
            msgs.sort(key=lambda m: m.message_id)
            data["album"] = msgs
            return await handler(msgs[0], data)

        entry["messages"].append(event)
        return None
