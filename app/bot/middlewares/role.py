"""
RoleFilter — фильтр проверки роли для защищённых хэндлеров.

Использование:
    from app.bot.middlewares.role import require_role
    from app.db.enums import UserRole

    @router.message(Command("approve"), require_role(UserRole.ADMIN))
    async def cmd_approve(message: Message, user: User): ...

Контракт:
- Если user отсутствует в data (не-приватный чат) — возвращает False молча.
- Если роль входит в разрешённые — True.
- Иначе — отвечает «Недостаточно прав для этой команды.» и возвращает False.
"""

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.db.enums import UserRole
from app.db.models import User


class RoleFilter(Filter):
    def __init__(self, *roles: UserRole) -> None:
        self.roles: set[str] = {r.value for r in roles}

    async def __call__(
        self,
        event: TelegramObject,
        user: User | None = None,
    ) -> bool:
        if user is None:
            return False
        if not user.is_active:
            await self._notify_denied(event)
            return False
        if user.role in self.roles:
            return True
        await self._notify_denied(event)
        return False

    @staticmethod
    async def _notify_denied(event: TelegramObject) -> None:
        text = "Недостаточно прав для этой команды."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)


def require_role(*roles: UserRole) -> RoleFilter:
    return RoleFilter(*roles)
