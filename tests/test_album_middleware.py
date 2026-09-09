"""
Тесты AlbumMiddleware — гарантия, что media_group из N сообщений сходится
в один вызов handler'а со списком в data["album"], а одиночные сообщения
проходят без изменений.

Корень: исторически N сообщений альбома триггерили N concurrent task'ов,
каждый делал get_data → append → update_data на одном FSM. Гонка —
последний write побеждал, остальные файлы терялись. Тесты ловят оба
симптома сразу: и потерю файлов (батч из N), и совместимость со
single-message сценарием.
"""

from __future__ import annotations

import asyncio

import pytest

from app.bot.middlewares.album import AlbumMiddleware


class _FakeMessage:
    """Минимальная подделка Message с теми атрибутами, что трогает middleware."""

    def __init__(self, message_id: int, media_group_id: str | None = None) -> None:
        self.message_id = message_id
        self.media_group_id = media_group_id


@pytest.mark.asyncio
async def test_single_message_passes_through() -> None:
    """Сообщение без media_group_id — handler вызван один раз, без album в data."""
    mw = AlbumMiddleware(delay=0.05)
    calls: list[tuple[_FakeMessage, dict]] = []

    async def handler(event, data):
        calls.append((event, data))
        return "ok"

    msg = _FakeMessage(message_id=1)
    result = await mw(handler, msg, {})

    assert result == "ok"
    assert len(calls) == 1
    assert calls[0][0] is msg
    assert "album" not in calls[0][1]


@pytest.mark.asyncio
async def test_album_of_three_collapses_to_one_call() -> None:
    """3 сообщения с одним media_group_id → handler вызван 1 раз со списком из 3."""
    mw = AlbumMiddleware(delay=0.2)
    calls: list[tuple[_FakeMessage, dict]] = []

    async def handler(event, data):
        calls.append((event, data))
        return "ok"

    mgid = "album-42"
    msgs = [_FakeMessage(message_id=i, media_group_id=mgid) for i in (10, 11, 12)]

    results = await asyncio.gather(
        mw(handler, msgs[0], {}),
        mw(handler, msgs[1], {}),
        mw(handler, msgs[2], {}),
    )

    # Только первый получает результат handler'а, остальные None (буферизованы).
    assert results.count("ok") == 1
    assert results.count(None) == 2

    assert len(calls) == 1
    event_msg, data = calls[0]
    album = data["album"]
    assert len(album) == 3
    assert [m.message_id for m in album] == [10, 11, 12]
    # handler получает первое (по message_id) сообщение альбома
    assert event_msg.message_id == 10


@pytest.mark.asyncio
async def test_album_messages_sorted_by_message_id() -> None:
    """Порядок прихода сообщений в TG не гарантирован — middleware сортирует."""
    mw = AlbumMiddleware(delay=0.2)
    calls: list[list[int]] = []

    async def handler(event, data):
        calls.append([m.message_id for m in data["album"]])

    mgid = "album-shuffle"
    # Сообщения «приходят» в обратном порядке.
    msgs = [_FakeMessage(message_id=i, media_group_id=mgid) for i in (52, 50, 51)]
    await asyncio.gather(*(mw(handler, m, {}) for m in msgs))

    assert calls == [[50, 51, 52]]


@pytest.mark.asyncio
async def test_two_albums_in_parallel_dont_mix() -> None:
    """Параллельные альбомы с разными media_group_id обрабатываются независимо."""
    mw = AlbumMiddleware(delay=0.2)
    calls: list[tuple[str, list[int]]] = []

    async def handler(event, data):
        calls.append((event.media_group_id, [m.message_id for m in data["album"]]))

    msgs = [
        _FakeMessage(message_id=1, media_group_id="A"),
        _FakeMessage(message_id=2, media_group_id="B"),
        _FakeMessage(message_id=3, media_group_id="A"),
        _FakeMessage(message_id=4, media_group_id="B"),
    ]
    await asyncio.gather(*(mw(handler, m, {}) for m in msgs))

    calls_by_group = {gid: ids for gid, ids in calls}
    assert calls_by_group == {"A": [1, 3], "B": [2, 4]}


@pytest.mark.asyncio
async def test_cache_cleared_after_album_dispatch() -> None:
    """После обработки альбома кэш middleware пуст — нет утечки."""
    mw = AlbumMiddleware(delay=0.05)

    async def handler(event, data):
        return None

    await asyncio.gather(
        mw(handler, _FakeMessage(1, "X"), {}),
        mw(handler, _FakeMessage(2, "X"), {}),
    )
    # Даём место event loop'у завершить cleanup в finally
    await asyncio.sleep(0)

    assert mw._cache == {}
