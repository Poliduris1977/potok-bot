# app/bot/telegram.py
# Telegram webhook handler.
# Получает update → нормализует → запускает бизнес-логику → отвечает.

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.config import settings
from app.models import IncomingMessage
from app.bot.dialog import process_message
from app.utils import hash_uid

logger = logging.getLogger(__name__)

_TG_API = f"https://telegram-proxy.cointoindex.workers.dev/bot{settings.telegram_bot_token}"


async def handle_update(body: dict[str, Any]) -> None:
    """Точка входа для Telegram webhook."""
    message = body.get("message") or body.get("edited_message")
    if not message:
        return  # callback_query, channel_post и т.д. — игнорируем

    text = message.get("text", "").strip()
    if not text:
        return  # фото, стикер, голос — пока игнорируем

    from_user = message.get("from", {})
    chat_id   = str(message["chat"]["id"])
    user_id   = str(from_user.get("id", chat_id))
    username  = from_user.get("username")
    first     = from_user.get("first_name", "")
    last      = from_user.get("last_name", "")
    display   = f"{first} {last}".strip() or username or user_id

    incoming = IncomingMessage(
        platform="telegram",
        user_id=user_id,
        username=username,
        display_name=display,
        text=text,
    )

    logger.info("Telegram [%s] %s: %s", hash_uid(user_id), display, text[:80])

    reply = await process_message(incoming)

    await send_message(chat_id=chat_id, text=reply)


async def send_message(chat_id: str, text: str) -> None:
    """Отправить сообщение пользователю через Telegram Bot API."""
    url = f"{_TG_API}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Telegram sendMessage ошибка %d: %s", resp.status, body)