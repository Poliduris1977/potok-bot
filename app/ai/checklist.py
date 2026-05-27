# app/ai/checklist.py
# Определяет что уже выяснено в диалоге.
# Анализирует последний ответ бота и текст клиента по ключевым словам.
# Не идеально — GigaChat сам тоже помогает отслеживать через промпт.

from __future__ import annotations

import re
from app.models import Lead, ChecklistProgress


# Паттерны для извлечения данных из текста клиента
_PHONE_RE    = re.compile(r"[\+7|8]?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
_BUDGET_RE   = re.compile(r"\d[\d\s]*(?:тыс|тысяч|млн|руб|рублей|к\b)", re.IGNORECASE)
_SIZE_RE     = re.compile(r"\d+[\s]*(?:кв|м2|метр|квадрат)", re.IGNORECASE)


CHECKLIST_LABELS = {
    "product":  "что хочет (кухня / шкаф / гардеробная / прихожая)",
    "size":     "размер помещения или примерные габариты",
    "style":    "стиль, цвет, предпочтения по материалам",
    "budget":   "бюджет",
    "deadline": "сроки — когда нужно, есть ли ремонт",
    "address":  "район или адрес для замера",
    "contact":  "имя и телефон",
}

# Ключевые слова для определения продукта
_PRODUCT_KEYWORDS = [
    "кухн", "шкаф", "гардероб", "прихожа", "столешниц",
    "встройк", "фасад", "фурнитур", "пенал",
]

# Ключевые слова для стиля
_STYLE_KEYWORDS = [
    "классик", "модерн", "минимализм", "хай-тек", "прованс",
    "белый", "серый", "чёрный", "дерево", "матов", "глянц",
    "mdf", "лдсп", "массив", "пластик", "эмаль",
]

# Ключевые слова для сроков
_DEADLINE_KEYWORDS = [
    "срочно", "месяц", "недел", "год", "ремонт", "новосел",
    "переезд", "когда", "успеть", "сроки", "до ",
]


def update_progress(
    lead: Lead,
    progress: ChecklistProgress,
    user_text: str,
) -> tuple[Lead, ChecklistProgress]:
    """
    Обновить лид и прогресс на основе последнего сообщения клиента.
    Возвращает обновлённые объекты.
    """
    text = user_text.lower()

    # Продукт
    if not progress.product:
        if any(kw in text for kw in _PRODUCT_KEYWORDS):
            progress.product = True
            # Сохранить первое упоминание продукта в лид
            if not lead.product:
                lead.product = _extract_product(text)

    # Размер
    if not progress.size and _SIZE_RE.search(user_text):
        progress.size = True

    # Стиль
    if not progress.style:
        if any(kw in text for kw in _STYLE_KEYWORDS):
            progress.style = True

    # Бюджет
    if not progress.budget:
        budget_match = _BUDGET_RE.search(user_text)
        if budget_match:
            progress.budget = True
            lead.budget = budget_match.group(0).strip()

    # Сроки
    if not progress.deadline:
        if any(kw in text for kw in _DEADLINE_KEYWORDS):
            progress.deadline = True

    # Адрес — если упомянул улицу, район, метро
    if not progress.address:
        if any(w in text for w in ["улиц", "проспект", "район", "метро", "переулок", "шоссе"]):
            progress.address = True
            lead.address = user_text[:200]  # сырой текст, потом уточнится

    # Контакт — телефон
    if not progress.contact:
        phone_match = _PHONE_RE.search(user_text)
        if phone_match:
            progress.contact = True
            lead.phone = phone_match.group(0).strip()

    # Имя — если клиент представился
    if not lead.name:
        name = _extract_name(user_text)
        if name:
            lead.name = name

    return lead, progress


def _extract_product(text: str) -> str:
    """Вернуть первый найденный тип продукта."""
    mapping = {
        "кухн": "Кухня",
        "шкаф": "Шкаф",
        "гардероб": "Гардеробная",
        "прихожа": "Прихожая",
        "столешниц": "Столешница",
    }
    for kw, label in mapping.items():
        if kw in text:
            return label
    return "Мебель на заказ"


def _extract_name(text: str) -> str | None:
    """Простое извлечение имени если клиент написал 'меня зовут X' или 'я X'."""
    patterns = [
        r"меня зовут\s+([А-ЯЁа-яёA-Za-z]+)",
        r"я\s+([А-ЯЁ][а-яё]+)",
        r"зовите меня\s+([А-ЯЁа-яёA-Za-z]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).capitalize()
    return None
