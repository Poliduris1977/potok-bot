# app/ai/tools.py
# Описания функций для GigaChat Function Calling + их обработчики.
# Агент вызывает эти функции сам — keyword-парсинг больше не нужен.

from __future__ import annotations

import json
import logging
from typing import Any

from app.models import Lead, ChecklistProgress
from app.ai.depersonalize import mask_for_log

logger = logging.getLogger(__name__)


# ── Схемы функций (передаются в GigaChat) ────────────────────────────────────

FUNCTIONS: list[dict[str, Any]] = [
    {
        "name": "save_lead_data",
        "description": (
            "Сохранить данные клиента, собранные в ходе диалога. "
            "Вызывать каждый раз как только клиент сообщил новый факт: "
            "имя, телефон, тип продукта, бюджет, адрес, стиль, размер, сроки. "
            "Передавать только те поля, которые клиент явно назвал."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name":     {"type": "string", "description": "Имя клиента"},
                "phone":    {"type": "string", "description": "Номер телефона"},
                "product":  {"type": "string", "description": "Что хочет: кухня / шкаф / гардеробная / прихожая"},
                "budget":   {"type": "string", "description": "Бюджет в рублях или диапазон"},
                "address":  {"type": "string", "description": "Район или адрес для замера"},
                "deadline": {"type": "string", "description": "Когда нужно, сроки ремонта"},
                "style":    {"type": "string", "description": "Стиль, цвет, материалы"},
                "size":     {"type": "string", "description": "Размер помещения или габариты"},
            },
            "required": [],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Записать клиента на бесплатный выезд дизайнера. "
            "Вызывать только когда клиент явно согласился на замер "
            "И назвал адрес И есть способ с ним связаться (телефон или имя). "
            "Не вызывать если клиент только интересуется или говорит 'может быть'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "address":   {"type": "string", "description": "Адрес замера"},
                "date_time": {"type": "string", "description": "Дата и время замера если клиент назвал"},
                "phone":     {"type": "string", "description": "Телефон для подтверждения"},
            },
            "required": ["address"],
        },
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Передать диалог живому специалисту компании. "
            "Вызывать если: клиент просит живого человека / менеджера / позвонить; "
            "вопрос про корпоративный заказ, застройщика, опт; "
            "клиент явно недоволен или агрессивен; "
            "5+ сообщений без прогресса."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Причина передачи: 'request' | 'corporate' | 'no_progress' | 'angry'",
                    "enum": ["request", "corporate", "no_progress", "angry"],
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "get_product_info",
        "description": (
            "Получить актуальную информацию о продуктах, ценах, материалах или сроках "
            "из базы знаний компании. Вызывать когда клиент спрашивает конкретные детали "
            "о продукте, материалах, комплектации или процессе работы."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Тема запроса: например 'цены на кухни', 'сроки изготовления', 'материалы фасадов'",
                },
            },
            "required": ["query"],
        },
    },
]


# ── Контекст агента (мутируется в процессе цикла) ─────────────────────────────

class AgentContext:
    """Хранит состояние агента в рамках одного обращения."""

    def __init__(self, lead: Lead, progress: ChecklistProgress):
        self.lead = lead
        self.progress = progress
        self.appointment_booked = False
        self.needs_transfer = False
        self.transfer_reason: str | None = None

    def to_result(self) -> "AgentResult":
        return AgentResult(
            lead=self.lead,
            progress=self.progress,
            appointment_booked=self.appointment_booked,
            needs_transfer=self.needs_transfer,
            transfer_reason=self.transfer_reason,
        )


class AgentResult:
    def __init__(
        self,
        lead: Lead,
        progress: ChecklistProgress,
        appointment_booked: bool,
        needs_transfer: bool,
        transfer_reason: str | None,
    ):
        self.lead = lead
        self.progress = progress
        self.appointment_booked = appointment_booked
        self.needs_transfer = needs_transfer
        self.transfer_reason = transfer_reason


# ── Обработчики функций ───────────────────────────────────────────────────────

def handle_save_lead_data(args: dict, ctx: AgentContext) -> str:
    """Обновить лид и прогресс чеклиста данными от агента."""
    lead = ctx.lead
    progress = ctx.progress

    if name := args.get("name"):
        lead.name = name
        logger.info("Агент сохранил имя: %s", mask_for_log(name))

    if phone := args.get("phone"):
        lead.phone = phone
        progress.contact = True
        logger.info("Агент сохранил телефон: %s", mask_for_log(phone))

    if product := args.get("product"):
        lead.product = product
        progress.product = True

    if budget := args.get("budget"):
        lead.budget = budget
        progress.budget = True

    if address := args.get("address"):
        lead.address = address
        progress.address = True

    if deadline := args.get("deadline"):
        lead.deadline = deadline
        progress.deadline = True

    if args.get("style"):
        progress.style = True

    if args.get("size"):
        progress.size = True

    filled = progress.filled_count
    logger.info("save_lead_data: заполнено %d/7 полей чеклиста", filled)
    return json.dumps({"status": "ok", "filled_count": filled}, ensure_ascii=False)


def handle_book_appointment(args: dict, ctx: AgentContext) -> str:
    """Зафиксировать готовность к записи на замер."""
    from app.leads.appointment import extract_datetime

    ctx.appointment_booked = True
    ctx.lead.address = args.get("address", ctx.lead.address)
    ctx.progress.address = True

    if phone := args.get("phone"):
        ctx.lead.phone = phone
        ctx.progress.contact = True

    if dt_str := args.get("date_time"):
        dt = extract_datetime(dt_str)
        if dt:
            ctx.lead.appointment_dt = dt

    logger.info("Агент записал на замер: адрес=%s", mask_for_log(ctx.lead.address or ""))
    return json.dumps({"status": "booked", "address": ctx.lead.address}, ensure_ascii=False)


def handle_transfer_to_human(args: dict, ctx: AgentContext) -> str:
    """Инициировать передачу живому специалисту."""
    reason = args.get("reason", "request")
    ctx.needs_transfer = True
    ctx.transfer_reason = reason
    logger.info("Агент инициировал передачу: reason=%s", reason)
    return json.dumps({"status": "transfer_initiated", "reason": reason}, ensure_ascii=False)


def handle_get_product_info(args: dict, ctx: AgentContext) -> str:
    """Вернуть информацию из базы знаний."""
    from app.knowledge.loader import get_knowledge

    query = args.get("query", "").lower()
    knowledge = get_knowledge()

    # Простой роутинг по теме запроса
    if any(w in query for w in ["цен", "стоим", "бюджет", "рубл", "тыс"]):
        section = knowledge.get("products", "Информация о ценах уточняется после замера.")
    elif any(w in query for w in ["срок", "изготовл", "производ", "когда"]):
        section = knowledge.get("workflow", "Сроки уточняются при оформлении заказа.")
    elif any(w in query for w in ["материал", "фасад", "лдсп", "mdf", "массив", "эмаль"]):
        section = knowledge.get("products", "Работаем с широким ассортиментом материалов.")
    elif any(w in query for w in ["возражен", "дорого", "подумаю", "посмотрю"]):
        section = knowledge.get("objections", "")
    else:
        section = knowledge.get("company", "")

    result = section[:800] if section else "Информация уточняется — менеджер свяжется с вами."
    return json.dumps({"info": result}, ensure_ascii=False)


# ── Диспетчер ─────────────────────────────────────────────────────────────────

HANDLERS = {
    "save_lead_data":    handle_save_lead_data,
    "book_appointment":  handle_book_appointment,
    "transfer_to_human": handle_transfer_to_human,
    "get_product_info":  handle_get_product_info,
}


def dispatch(function_name: str, arguments_json: str, ctx: AgentContext) -> str:
    """Вызвать обработчик функции и вернуть JSON-результат."""
    handler = HANDLERS.get(function_name)
    if not handler:
        logger.error("Неизвестная функция агента: %s", function_name)
        return json.dumps({"error": f"unknown function: {function_name}"})

    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        logger.warning(
            "dispatch: невалидный JSON для %s — используем пустые args. Error: %s",
            function_name, e,
        )
        args = {}

    return handler(args, ctx)