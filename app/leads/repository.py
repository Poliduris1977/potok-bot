# app/leads/repository.py
# Все операции с БД для лидов, диалогов и сообщений.
# Один модуль — одна ответственность: хранение данных.

from __future__ import annotations

import json
import logging
from datetime import datetime

from app.database import get_db
from app.models import Lead, Dialog, Message, ChecklistProgress, Platform

logger = logging.getLogger(__name__)


# ── Лиды ──────────────────────────────────────────────────────────────────────

async def get_or_create_lead(user_id: str, platform: Platform, username: str | None = None) -> Lead:
    async with get_db() as conn:
        row = await conn.fetchrow(
            """INSERT INTO leads (user_id, platform, username, status)
               VALUES ($1, $2, $3, 'new')
               ON CONFLICT (user_id, platform) DO UPDATE
                 SET username = COALESCE(EXCLUDED.username, leads.username)
               RETURNING *""",
            user_id, platform, username
        )
        logger.info("Лид получен/создан: %s / %s", platform, user_id)
        return Lead(**dict(row))


async def update_lead(lead: Lead) -> Lead:
    async with get_db() as conn:
        row = await conn.fetchrow(
            """UPDATE leads SET
                name=$1, phone=$2, address=$3, budget=$4,
                deadline=$5, product=$6, status=$7,
                appointment_dt=$8, notes=$9,
                size=$10, style=$11, updated_at=NOW()
               WHERE id=$12 RETURNING *""",
            lead.name, lead.phone, lead.address, lead.budget,
            lead.deadline, lead.product, lead.status,
            lead.appointment_dt, lead.notes,
            lead.size, lead.style, lead.id
        )
        return Lead(**dict(row))


# ── Диалоги ───────────────────────────────────────────────────────────────────

async def get_or_create_dialog(lead: Lead) -> Dialog:
    async with get_db() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM dialogs
               WHERE user_id=$1 AND platform=$2 AND transferred=FALSE
               ORDER BY started_at DESC LIMIT 1""",
            lead.user_id, lead.platform
        )
        if row:
            return Dialog(**dict(row))

        row = await conn.fetchrow(
            """INSERT INTO dialogs (lead_id, user_id, platform, started_at, last_message_at)
               VALUES ($1, $2, $3, NOW(), NOW()) RETURNING *""",
            lead.id, lead.user_id, lead.platform
        )
        return Dialog(**dict(row))


async def increment_dialog_messages(dialog_id: int) -> None:
    async with get_db() as conn:
        await conn.execute(
            "UPDATE dialogs SET message_count=message_count+1, last_message_at=NOW() WHERE id=$1",
            dialog_id
        )


async def mark_dialog_transferred(dialog_id: int) -> None:
    async with get_db() as conn:
        await conn.execute(
            "UPDATE dialogs SET transferred=TRUE WHERE id=$1",
            dialog_id
        )


# ── Сообщения ─────────────────────────────────────────────────────────────────

async def save_message(dialog_id: int, role: str, content: str) -> None:
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO messages (dialog_id, role, content) VALUES ($1, $2, $3)",
            dialog_id, role, content
        )


async def get_dialog_messages(dialog_id: int, limit: int = 20) -> list[dict[str, str]]:
    """Получить последние N сообщений диалога для передачи в GigaChat."""
    async with get_db() as conn:
        rows = await conn.fetch(
            """SELECT role, content FROM messages
               WHERE dialog_id=$1
               ORDER BY created_at DESC LIMIT $2""",
            dialog_id, limit
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ── Прогресс чеклиста ─────────────────────────────────────────────────────────

async def get_checklist_progress(lead_id: int) -> ChecklistProgress:
    async with get_db() as conn:
        row = await conn.fetchrow("SELECT notes FROM leads WHERE id=$1", lead_id)
        if not row or not row["notes"]:
            return ChecklistProgress()
        try:
            data = json.loads(row["notes"])
            checklist = data.get("checklist", {})
            return ChecklistProgress(**checklist)
        except Exception as e:
            logger.warning("[REPO] Не удалось прочитать checklist lead_id=%s: %s",
                           lead_id, type(e).__name__)
            return ChecklistProgress()


async def save_checklist_progress(lead_id: int, progress: ChecklistProgress) -> None:
    """Атомарно сохранить чеклист в notes через JSONB merge — без race condition."""
    async with get_db() as conn:
        await conn.execute(
            """UPDATE leads
               SET notes = COALESCE(notes::jsonb, '{}'::jsonb) || $1::jsonb,
                   updated_at = NOW()
               WHERE id = $2""",
            json.dumps({"checklist": progress.model_dump()}, ensure_ascii=False),
            lead_id,
        )


# ── Удаление лида (152-ФЗ, право на забвение) ─────────────────────────────────

async def delete_lead_cascade(pool, lead_id: int) -> bool:
    """
    Каскадно удалить лид и все связанные данные.

    Порядок важен из-за FK-ограничений:
      messages → dialogs → appointments → leads

    Returns:
        True  — лид найден и удалён
        False — лид не найден
    """
    async with pool.acquire(timeout=5.0) as conn:
        async with conn.transaction():

            exists = await conn.fetchval(
                "SELECT id FROM leads WHERE id = $1",
                lead_id
            )
            if not exists:
                return False

            await conn.execute(
                """DELETE FROM messages
                   WHERE dialog_id IN (
                       SELECT id FROM dialogs WHERE lead_id = $1
                   )""",
                lead_id
            )

            await conn.execute(
                "DELETE FROM dialogs WHERE lead_id = $1",
                lead_id
            )

            await conn.execute(
                "DELETE FROM appointments WHERE lead_id = $1",
                lead_id
            )

            await conn.execute(
                "DELETE FROM leads WHERE id = $1",
                lead_id
            )

    logger.info("[REPO] Лид %d каскадно удалён (152-ФЗ)", lead_id)
    return True