# app/models.py
# Pydantic v2 модели — используются для валидации и передачи данных между модулями.
# Не ORM. Работают вместе с asyncpg (asyncpg возвращает Record, мы конвертируем).

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict


# ── Платформы ─────────────────────────────────────────────────────────────────

Platform = Literal["telegram", "max"]
LeadStatus = Literal["new", "hot", "booked", "transferred"]
MessageRole = Literal["user", "assistant"]


# ── Нормализованное входящее сообщение ───────────────────────────────────────
# Оба адаптера (Telegram / Макс) приводят входящее к этому формату
# до того, как передать в бизнес-логику.

class IncomingMessage(BaseModel):
    platform: Platform
    user_id: str
    username: str | None = None
    display_name: str | None = None
    text: str


# ── Лид ───────────────────────────────────────────────────────────────────────

class Lead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    platform: Platform
    user_id: str
    username: str | None = None
    name: str | None = None
    phone: str | None = None
    phone_channel: str | None = None   # Zadarma — номер для карточки лида
    address: str | None = None
    budget: str | None = None
    deadline: str | None = None
    product: str | None = None
    size: str | None = None
    style: str | None = None
    status: LeadStatus = "new"
    appointment_dt: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Диалог ────────────────────────────────────────────────────────────────────

class Dialog(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    lead_id: int | None = None
    platform: Platform
    user_id: str
    started_at: datetime | None = None
    last_message_at: datetime | None = None
    message_count: int = 0
    transferred: bool = False


# ── Сообщение в истории ───────────────────────────────────────────────────────

class Message(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    dialog_id: int | None = None
    role: MessageRole
    content: str
    created_at: datetime | None = None


# ── Запись на замер ───────────────────────────────────────────────────────────

class Appointment(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    lead_id: int | None = None
    scheduled_at: datetime | None = None
    address: str | None = None
    confirmed: bool = False
    created_at: datetime | None = None


# ── Прогресс чеклиста ─────────────────────────────────────────────────────────
# Передаётся в системный промпт как контекст что уже выяснено

class ChecklistProgress(BaseModel):
    product: bool = False
    size: bool = False
    style: bool = False
    budget: bool = False
    deadline: bool = False
    address: bool = False
    contact: bool = False

    @property
    def filled_count(self) -> int:
        return sum([
            self.product, self.size, self.style,
            self.budget, self.deadline, self.address, self.contact
        ])

    @property
    def is_hot(self) -> bool:
        return self.filled_count >= 4

    @property
    def is_booked(self) -> bool:
        return self.contact