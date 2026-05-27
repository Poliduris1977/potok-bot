# app/leads/notifier.py
# Уведомления директору в Telegram и MAX (параллельно).
# Три типа: горячий лид, запись на замер, передача специалисту.
#
# Telegram — основной канал (DIRECTOR_CHAT_ID).
# MAX       — резервный канал (DIRECTOR_MAX_ID), на случай блокировки Telegram.
# Оба отправляются одновременно через asyncio.gather().
# Падение одного канала не влияет на другой.
#
# Безопасность:
#   — Токен бота формируется локально внутри _send_telegram() — не хранится в модуле
#   — Все пользовательские данные экранируются через html.escape() — HTML injection невозможен
#   — Телефон маскируется в уведомлениях — частичная защита 152-ФЗ при передаче через Telegram
#   ⚠️  152-ФЗ: уведомления с ПД идут через Telegram (иностранный сервис).
#       Для полного соответствия — добавить согласие клиента на обработку через мессенджер.

from __future__ import annotations

import asyncio
import html
import logging
import re

import aiohttp

from app.config import settings
from app.models import Lead, ChecklistProgress
from app.utils import hash_uid

logger = logging.getLogger(__name__)

_MAX_API       = "https://platform-api.max.ru"
_TG_ERROR_TRIM = 200   # символов — максимум тела ошибки в логе


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _esc(value: str | None) -> str:
    """Экранировать HTML-спецсимволы в пользовательских данных."""
    return html.escape(str(value)) if value else "—"


def _mask_phone(phone: str | None) -> str:
    """
    Маскировать телефон по позициям цифр — не по позиции символов в строке.
    +7 921 111-22-33 → +7 921 111-**-**
    Маскируем последние 4 цифры независимо от форматирования.
    """
    if not phone:
        return "—"
    # Находим позиции всех цифр в строке
    digit_positions = [i for i, ch in enumerate(phone) if ch.isdigit()]
    if len(digit_positions) < 4:
        return "***"
    # Маскируем последние 4 цифры, остальное оставляем как есть
    mask_from = digit_positions[-4]
    result = list(phone)
    for i in digit_positions[-4:]:
        result[i] = "*"
    return "".join(result)


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def notify_if_needed(
    lead: Lead,
    progress: ChecklistProgress,
    needs_transfer: bool,
    prev_status: str,
) -> None:
    if not settings.director_chat_id and not settings.director_max_id:
        return

    if lead.status == "booked" and prev_status != "booked":
        await _send(lead, "booked")
        return

    if needs_transfer and prev_status != "transferred":
        await _send(lead, "transfer")
        return

    if lead.status == "hot" and prev_status == "new":
        await _send(lead, "hot_lead")


async def notify_director_error(user_id: str, error: str) -> None:
    """Уведомление при падении обоих AI-провайдеров."""
    # user_id и error — внутренние данные, не ПД клиента — экранируем для безопасности
    text = (
        "🔴 <b>БОТ НЕДОСТУПЕН</b>\n\n"
        "Оба провайдера не отвечают.\n"
        f"Пользователь: {html.escape(str(user_id))}\n"
        f"Причина: {html.escape(str(error)[:100])}"
    )
    await asyncio.gather(
        _send_telegram(text),
        _send_max(text) if settings.director_max_id else asyncio.sleep(0),
        return_exceptions=True,
    )


# ── Формирование текста ───────────────────────────────────────────────────────

def _make_contact(lead: Lead) -> str:
    """Ссылка/контакт для связи с клиентом в зависимости от платформы."""
    if lead.platform == "telegram":
        if lead.username:
            return f"@{html.escape(lead.username)}"
        return f'<a href="tg://user?id={html.escape(str(lead.user_id))}">открыть чат</a>'
    elif lead.platform == "max":
        if lead.username:
            return f"https://max.ru/@{html.escape(lead.username)}"
        return f"ID {html.escape(str(lead.user_id))} (найти в MAX вручную)"
    return html.escape(str(lead.user_id)) if lead.user_id else "—"


async def _send(lead: Lead, reason: str) -> None:
    templates = {
        "booked": (
            "✅ <b>ЗАПИСЬ НА ЗАМЕР</b>\n\n"
            "👤 Имя: {name}\n"
            "📞 Телефон: {phone}\n"   # маскирован
            "📍 Адрес: {address}\n"
            "📅 Дата: {appointment_dt}\n"
            "🛋 Запрос: {product}\n"
            "💰 Бюджет: {budget}\n"
            "⏱ Сроки: {deadline}\n"
            "📱 Платформа: {platform}\n"
            "🔗 Контакт: {contact}"
        ),
        "hot_lead": (
            "🔥 <b>ГОРЯЧИЙ ЛИД</b>\n\n"
            "👤 Имя: {name}\n"
            "🛋 Запрос: {product}\n"
            "💰 Бюджет: {budget}\n"
            "⏱ Сроки: {deadline}\n"
            "📱 Платформа: {platform}\n"
            "🔗 Контакт: {contact}"
        ),
        "transfer": (
            "⚡️ <b>НУЖЕН ЖИВОЙ СПЕЦИАЛИСТ</b>\n\n"
            "👤 Имя: {name}\n"
            "🛋 Запрос: {product}\n"
            "💰 Бюджет: {budget}\n"
            "📱 Платформа: {platform}\n"
            "🔗 Контакт: {contact}"
        ),
    }

    # Все пользовательские данные экранируются — HTML injection невозможен
    text = templates[reason].format(
        name=_esc(lead.name),
        phone=_mask_phone(lead.phone),          # маскируем для 152-ФЗ
        address=_esc(lead.address),
        appointment_dt=_esc(str(lead.appointment_dt) if lead.appointment_dt else None),
        product=_esc(lead.product),
        budget=_esc(lead.budget),
        deadline=_esc(lead.deadline),
        platform=_esc(lead.platform),
        contact=_make_contact(lead),
    )

    tasks = []
    if settings.director_chat_id:
        tasks.append(_send_telegram(text))
    if settings.director_max_id:
        tasks.append(_send_max(text))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Проверяем результаты — gather с return_exceptions=True молча поглощает ошибки
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            channel = "TG" if i == 0 else "MAX"
            logger.error("[NOTIFY] Канал %s недоступен: %s", channel, type(result).__name__)
    logger.info("Директор уведомлён: %s / лид %s", reason, hash_uid(lead.user_id))


# ── Транспорты ────────────────────────────────────────────────────────────────

async def _send_telegram(text: str) -> None:
    # Токен формируется локально — не хранится на уровне модуля
    tg_api = f"https://telegram-proxy.cointoindex.workers.dev/bot{settings.telegram_bot_token}"
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{tg_api}/sendMessage",
                json={
                    "chat_id": settings.director_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status != 200:
                body = await resp.text()
                # Обрезаем тело ответа — не логируем потенциально чувствительные данные
                logger.error(
                    "[TG] Уведомление не отправлено %d: %s",
                    resp.status, body[:_TG_ERROR_TRIM],
                )
            else:
                logger.debug("[TG] Уведомление отправлено")
    except Exception as e:
        logger.error("[TG] Ошибка отправки: %s", type(e).__name__)


async def _send_max(text: str) -> None:
    # MAX не поддерживает HTML-теги — очищаем перед отправкой
    clean_text = re.sub(r"<[^>]+>", "", text)

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{_MAX_API}/messages",
                headers={
                    "Authorization": settings.max_bot_token,
                    "Content-Type": "application/json",
                },
                params={"user_id": settings.director_max_id},
                json={"text": clean_text},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.error(
                    "[MAX] Уведомление не отправлено %d: %s",
                    resp.status, body[:_TG_ERROR_TRIM],
                )
            else:
                logger.debug("[MAX] Уведомление отправлено")
    except Exception as e:
        logger.error("[MAX] Ошибка отправки: %s", type(e).__name__)