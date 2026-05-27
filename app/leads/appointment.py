# app/leads/appointment.py
# Логика записи на замер.
# Определяет когда клиент готов к записи и сохраняет встречу в БД.

from __future__ import annotations

import logging
import re
from datetime import datetime

from app.database import get_db
from app.models import Lead, Appointment

logger = logging.getLogger(__name__)

# Ключевые фразы готовности к замеру
_READY_PHRASES = [
    "запишите", "запиши", "хочу замер", "нужен замер",
    "давайте замер", "приезжайте", "можно замер",
    "оформите", "договорились", "согласен", "согласна",
    "давайте", "ок", "окей", "хорошо", "отлично",
]

# Паттерны для извлечения даты из текста
_DATE_PATTERNS = [
    r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?",   # 15.06, 15-06-2025
    r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)",
]

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Паттерн телефона
_PHONE_RE = re.compile(
    r"(?:\+7|8)?[\s\-]?\(?(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})"
)


def is_ready_for_appointment(text: str) -> bool:
    """Проверить — клиент согласился на замер?"""
    text_lower = text.lower().strip()
    return any(phrase in text_lower for phrase in _READY_PHRASES)


def extract_phone(text: str) -> str | None:
    """Извлечь номер телефона из текста."""
    m = _PHONE_RE.search(text)
    if m:
        return f"+7 ({m.group(1)}) {m.group(2)}-{m.group(3)}-{m.group(4)}"
    return None


def extract_datetime(text: str) -> datetime | None:
    """Попытаться извлечь дату из текста."""
    now = datetime.now()

    for pattern in _DATE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        try:
            groups = m.groups()
            if groups[1] in _MONTHS:
                # "15 июня"
                day = int(groups[0])
                month = _MONTHS[groups[1]]
                year = now.year
                dt = datetime(year, month, day, 10, 0)
                # Если дата уже прошла — следующий год
                if dt < now:
                    dt = datetime(year + 1, month, day, 10, 0)
                return dt
            else:
                # "15.06" или "15.06.2025"
                day = int(groups[0])
                month = int(groups[1])
                year = int(groups[2]) if groups[2] else now.year
                if year < 100:
                    year += 2000
                return datetime(year, month, day, 10, 0)
        except (ValueError, TypeError):
            continue
    return None


async def save_appointment(lead: Lead, scheduled_at: datetime | None, address: str | None) -> Appointment:
    """Сохранить запись на замер в БД."""
    async with get_db() as conn:
        row = await conn.fetchrow(
            """INSERT INTO appointments (lead_id, scheduled_at, address, confirmed)
               VALUES ($1, $2, $3, FALSE) RETURNING *""",
            lead.id, scheduled_at, address
        )
        logger.info(
            "Замер записан: лид %s, дата %s, адрес %s",
            lead.id, scheduled_at, address
        )
        return Appointment(**dict(row))


async def get_appointment(lead_id: int) -> Appointment | None:
    """Получить последнюю запись на замер для лида."""
    async with get_db() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM appointments WHERE lead_id=$1 ORDER BY created_at DESC LIMIT 1",
            lead_id
        )
        return Appointment(**dict(row)) if row else None
