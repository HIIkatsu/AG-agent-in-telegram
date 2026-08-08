"""Auth middleware — strict dual filtering by FORUM_GROUP_ID + ALLOWED_USER_IDS.

Drops absolutely everything (messages, callbacks, forum events) that arrives
from the wrong group or from an unauthorized user.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.config import settings

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """Hard dual gate: group ID + user whitelist.

    - Any event from a chat whose ``id != FORUM_GROUP_ID`` → silently dropped.
    - Any event whose sender is not in ``ALLOWED_USER_IDS`` → silently dropped.
    - Forum service messages (topic closed/reopened) may lack ``from_user``
      but still pass if they originate from the correct group.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # ── 1. Resolve chat ID ──────────────────────────────────────────
        chat_id: int | None = None
        if isinstance(event, Message) and event.chat:
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id  # type: ignore[union-attr]

        # Gate: wrong group → drop silently
        if settings.forum_group_id and chat_id != settings.forum_group_id:
            logger.debug("Dropped event from chat %s (expected %s)", chat_id, settings.forum_group_id)
            return None

        # ── 2. Resolve user ─────────────────────────────────────────────
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        # Forum service messages (topic closed/created) may have no from_user.
        # If we already validated the group, let them through.
        if user is None:
            # Only allow if group was already validated
            if chat_id == settings.forum_group_id:
                return await handler(event, data)
            return None

        # Gate: unauthorized user → drop silently
        if user.id not in settings.allowed_ids:
            logger.debug("Dropped event from user %s (not in allowed list)", user.id)
            return None

        return await handler(event, data)
