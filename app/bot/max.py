# app/bot/max.py
# MAX Bot API adapter.
# Docs: https://dev.max.ru/docs-api/
#
# Key differences from the old botapi.max.ru:
#   - Base URL: https://platform-api.max.ru
#   - Auth header: "Authorization: {token}"  (NO "Bearer" prefix)
#   - Send:  POST /messages?user_id={user_id}  with {"text": "..."}
#   - Poll:  GET  /updates?timeout=30&marker={marker}

from __future__ import annotations

import logging

import aiohttp

from app.models import IncomingMessage
from app.bot.dialog import process_message

logger = logging.getLogger(__name__)

MAX_API_BASE = "https://platform-api.max.ru"


# -- Parse incoming update ----------------------------------------------------

def _parse_update(body: dict) -> IncomingMessage | None:
    """
    Normalize MAX event into IncomingMessage.

    MAX webhook / polling format:
    {
      "update_type": "message_created",
      "timestamp": 1234567890,
      "message": {
        "sender": {"user_id": 123, "name": "Ivan"},
        "body":   {"text": "Hello"},
        "timestamp": 1234567890
      }
    }
    """
    update_type = body.get("update_type") or body.get("type", "")

    if update_type == "bot_started":
        # /start equivalent
        message = body.get("message", {})
        sender  = message.get("sender", {})
        return IncomingMessage(
            platform="max",
            user_id=str(sender.get("user_id", "")),
            username=sender.get("username"),       # None если не задан
            display_name=sender.get("name"),
            text="/start",
        )

    if update_type != "message_created":
        logger.debug("MAX: skip update_type='%s'", update_type)
        return None

    message = body.get("message", {})
    sender  = message.get("sender", {})
    text    = (message.get("body", {}).get("text") or "").strip()

    if not text:
        return None

    return IncomingMessage(
        platform="max",
        user_id=str(sender.get("user_id", "")),
        username=sender.get("username"),       # None если не задан
        display_name=sender.get("name"),
        text=text,
    )


# -- Send reply ----------------------------------------------------------------

async def _send_message(user_id: str, text: str, token: str) -> None:
    """
    POST https://platform-api.max.ru/messages?user_id={user_id}
    Authorization: {token}   <-- no "Bearer" prefix
    """
    url     = f"{MAX_API_BASE}/messages"
    headers = {
        "Authorization": token,          # MAX uses raw token, not "Bearer ..."
        "Content-Type": "application/json",
    }
    params  = {"user_id": user_id}
    payload = {"text": text}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    err = await resp.text()
                    logger.error("MAX send → %s: %s", resp.status, err)
                else:
                    logger.debug("MAX send OK user_id=%s", user_id)
    except Exception:
        logger.exception("MAX: send error user_id=%s", user_id)


# -- Entry point (called from main.py webhook) --------------------------------

async def handle_update(body: dict) -> None:
    """
    Called from main.py:
        body = await request.json()
        await handle_update(body)
    """
    from app.config import settings

    msg = _parse_update(body)
    if msg is None:
        return

    reply = await process_message(msg)
    await _send_message(msg.user_id, reply, token=settings.max_bot_token)