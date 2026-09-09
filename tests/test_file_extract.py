"""
Тесты collect_files_from_messages — батч-приём файлов с учётом лимитов
по количеству (max_count) и размеру (MAX_FILE_SIZE).
"""

from __future__ import annotations



from app.bot.utils.file_extract import (
    MAX_FILE_SIZE,
    collect_files_from_messages,
    file_full_from_message,
    file_short_from_message,
)


class _FakeDoc:
    def __init__(self, *, file_id: str, file_size: int | None, name: str | None = "x.pdf"):
        self.file_id = file_id
        self.file_unique_id = f"u-{file_id}"
        self.file_name = name
        self.file_size = file_size
        self.mime_type = "application/pdf"


class _FakePhoto:
    def __init__(self, *, file_id: str, file_size: int | None):
        self.file_id = file_id
        self.file_unique_id = f"u-{file_id}"
        self.file_size = file_size


class _FakeMessage:
    def __init__(
        self,
        *,
        document=None,
        photo=None,
        video=None,
        animation=None,
    ):
        self.document = document
        self.photo = photo or None
        self.video = video
        self.animation = animation


def _doc_msg(file_id: str, size: int | None = 1024) -> _FakeMessage:
    return _FakeMessage(document=_FakeDoc(file_id=file_id, file_size=size))


def _photo_msg(file_id: str, size: int | None = 2048) -> _FakeMessage:
    return _FakeMessage(photo=[_FakePhoto(file_id=file_id, file_size=size)])


def test_short_extractor_doc() -> None:
    f = file_short_from_message(_doc_msg("d1"))
    assert f == {"kind": "document", "file_id": "d1", "file_name": "x.pdf"}


def test_short_extractor_photo_takes_largest() -> None:
    photos = [
        _FakePhoto(file_id="small", file_size=100),
        _FakePhoto(file_id="large", file_size=10000),
    ]
    msg = _FakeMessage(photo=photos)
    f = file_short_from_message(msg)
    assert f == {"kind": "photo", "file_id": "large", "file_name": None}


def test_full_extractor_doc_includes_mime_and_size() -> None:
    f = file_full_from_message(_doc_msg("d2", size=5555))
    assert f["kind"] == "document"
    assert f["file_id"] == "d2"
    assert f["mime_type"] == "application/pdf"
    assert f["size"] == 5555


def test_collect_accepts_batch_within_limit() -> None:
    msgs = [_photo_msg("p1"), _photo_msg("p2"), _photo_msg("p3")]
    accepted, oversize, overflow = collect_files_from_messages(
        msgs, extractor=file_short_from_message, current_count=0, max_count=10
    )
    assert [a["file_id"] for a in accepted] == ["p1", "p2", "p3"]
    assert oversize == 0
    assert overflow == 0


def test_collect_rejects_oversize() -> None:
    msgs = [
        _photo_msg("ok", size=1000),
        _photo_msg("toobig", size=MAX_FILE_SIZE + 1),
        _photo_msg("ok2", size=1000),
    ]
    accepted, oversize, overflow = collect_files_from_messages(
        msgs, extractor=file_short_from_message, current_count=0, max_count=10
    )
    assert [a["file_id"] for a in accepted] == ["ok", "ok2"]
    assert oversize == 1
    assert overflow == 0


def test_collect_overflow_partial_acceptance() -> None:
    """Уже 8 файлов в FSM, приходит альбом из 5 → принимаем первые 2, отказ 3."""
    msgs = [_photo_msg(f"p{i}") for i in range(5)]
    accepted, oversize, overflow = collect_files_from_messages(
        msgs, extractor=file_short_from_message, current_count=8, max_count=10
    )
    assert [a["file_id"] for a in accepted] == ["p0", "p1"]
    assert overflow == 3
    assert oversize == 0


def test_collect_size_unknown_is_accepted() -> None:
    """Telegram иногда не сообщает file_size (sticker-like, edge case) → принимаем."""
    msgs = [_photo_msg("no-size", size=None)]
    accepted, oversize, _ = collect_files_from_messages(
        msgs, extractor=file_short_from_message, current_count=0, max_count=10
    )
    assert len(accepted) == 1
    assert oversize == 0


def test_collect_mixed_oversize_and_overflow() -> None:
    msgs = [
        _photo_msg("ok1", size=1000),
        _photo_msg("toobig", size=MAX_FILE_SIZE + 1),
        _photo_msg("ok2", size=1000),
        _photo_msg("ok3", size=1000),
    ]
    accepted, oversize, overflow = collect_files_from_messages(
        msgs, extractor=file_short_from_message, current_count=9, max_count=10
    )
    # current=9, max=10 → ёмкость 1. ok1 примут, toobig — oversize,
    # ok2/ok3 — overflow.
    assert [a["file_id"] for a in accepted] == ["ok1"]
    assert oversize == 1
    assert overflow == 2
