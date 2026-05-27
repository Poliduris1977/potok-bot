# app/bot/dialog.py
from __future__ import annotations

import logging

from app.models import IncomingMessage
from app.ai.agent import run_agent
from app.leads.repository import (
    get_or_create_lead,
    get_or_create_dialog,
    get_dialog_messages,
    save_message,
    increment_dialog_messages,
    update_lead,
    get_checklist_progress,
    save_checklist_progress,
    mark_dialog_transferred,
)
from app.leads.notifier import notify_if_needed
from app.leads.appointment import save_appointment
from app.utils import hash_uid

logger = logging.getLogger(__name__)

_GREETING = (
    "Здравствуйте! Я {bot_name}, дизайнер-консультант компании {company}. "
    "Помогу подобрать кухню или мебель под ваш проект. "
    "Расскажите — что вас интересует?"
)


async def process_message(msg: IncomingMessage) -> str:
    from app.config import settings

    # Telegram в группах добавляет имя бота: /start@MyBot → нормализуем
    command = msg.text.strip().lower().split("@")[0]
    if command == "/start":
        # Сбрасываем SESSION_CACHE — новый диалог начинается чисто
        from app.ai.session import reset_session
        reset_session(f"{msg.platform}:{msg.user_id}")
        return _GREETING.format(
            bot_name=settings.bot_name,
            company=settings.company_name,
        )

    # Санитизация: null-байты ломают PostgreSQL TEXT поля
    msg = msg.model_copy(update={"text": msg.text.replace("\x00", "")})

    # Защита от ReDoS и спама — обрезаем сверхдлинные сообщения
    if len(msg.text) > 2000:
        logger.warning("[DIALOG] user=%s сообщение обрезано до 2000 символов", hash_uid(msg.user_id))
        msg = msg.model_copy(update={"text": msg.text[:2000]})

    lead = await get_or_create_lead(
        user_id=msg.user_id,
        platform=msg.platform,
        username=msg.username,
    )
    prev_status = lead.status
    if not lead.name and msg.display_name:
        lead.name = msg.display_name
    if not lead.username and msg.username:
        lead.username = msg.username

    dialog = await get_or_create_dialog(lead)
    progress = await get_checklist_progress(lead.id)

    await save_message(dialog.id, "user", msg.text)
    await increment_dialog_messages(dialog.id)

    history = await get_dialog_messages(dialog.id, limit=20)

    reply, agent_result = await run_agent(
        messages=history,
        lead=lead,
        progress=progress,
        session_key=f"{msg.platform}:{msg.user_id}",
    )

    lead     = agent_result.lead
    progress = agent_result.progress

    await save_message(dialog.id, "assistant", reply)

    if progress.is_hot and lead.status == "new":
        lead.status = "hot"
        logger.info("Lead became hot: %s", hash_uid(msg.user_id))

    if agent_result.appointment_booked and lead.status in ("new", "hot"):
        lead.status = "booked"
        await save_appointment(lead, lead.appointment_dt, lead.address)
        logger.info("Appointment booked for lead: %s", hash_uid(msg.user_id))

    if agent_result.needs_transfer and lead.status != "booked":
        lead.status = "transferred"
        await mark_dialog_transferred(dialog.id)
        logger.info("Transfer to specialist: %s reason=%s", hash_uid(msg.user_id), agent_result.transfer_reason)

    # Логируем любую смену статуса — независимо от источника (dialog или dispatch)
    if lead.status != prev_status:
        logger.info("Статус лида изменился: %s → %s (user=%s)", prev_status, lead.status, hash_uid(msg.user_id))

    await update_lead(lead)
    await save_checklist_progress(lead.id, progress)

    try:
        await notify_if_needed(
            lead, progress,
            agent_result.needs_transfer,
            prev_status,
        )
    except Exception as e:
        logger.error("[DIALOG] notify_if_needed failed: %s", type(e).__name__)

    return reply