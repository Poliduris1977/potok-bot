# app/ai/depersonalize.py
# Слой деперсонализации персональных данных (152-ФЗ).
#
# Применяется ТОЛЬКО для запросов к DeepSeek (иностранный сервис).
# GigaChat (российский сервис) — данные передаются как есть.
# В БД всегда хранится оригинал — деперсонализация только в памяти.
#
# Схема:
#   1. depersonalize(user_id, text)          — заменяет ПД на токены
#   2. personalize(user_id, text)            — восстанавливает оригиналы
#   3. depersonalize_history(user_id, msgs)  — обрабатывает историю диалога
#   4. mask_for_log(value)                   — маскирует ПД для логов
#
# Токены нумеруются: [ТЕЛЕФОН_0], [ТЕЛЕФОН_1] — корректная обработка
# нескольких значений одного типа в одном диалоге.
#
# Безопасность:
#   — Unicode-нормализация перед паттернами (защита от обхода через \u00a0 и др.)
#   — re.sub с callback — корректная замена без сдвига позиций
#   — Обратный индекс value→token — O(1) вместо O(n) поиска дублей
#   — get_session вызывается один раз за вызов depersonalize()
#   — Лимит длины сообщения MAX_MESSAGE_LEN — защита от ReDoS
#   — При переполнении pii_map выбрасывается RuntimeError → возврат пустой строки

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import NamedTuple

from app.ai.session import add_pii, get_session
from app.utils import hash_uid

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

MAX_MESSAGE_LEN = 2000   # символов — защита от ReDoS и спама


# ── Паттерны ──────────────────────────────────────────────────────────────────

class _Pattern(NamedTuple):
    regex:    re.Pattern
    label:    str
    is_group: bool   # True — извлекать group(1), False — весь match


# Телефон: +7 921 111-22-33 / 8(921)111-22-33 / 89211112233
_PHONE = re.compile(
    r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
)

# Email: user@mail.ru / ivan.petrov+work@gmail.com
_EMAIL = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
)

# Адрес — улица/проспект/переулок/бульвар + текст до 50 символов
_ADDRESS_STREET = re.compile(
    r'(?:ул\.?\s+|улица\s+|пр\.?\s+|пр-т\.?\s+|проспект\s+'
    r'|пер\.?\s+|переулок\s+|бул\.?\s+|бульвар\s+)'
    r'[А-Яа-яЁёa-zA-Z0-9\s\.,\-]{3,50}',
    re.IGNORECASE,
)

# Номер квартиры / дома / корпуса
_ADDRESS_FLAT = re.compile(
    r'(?:кв\.?|квартира|(?<!\w)д\.?(?=\s*\d)|дом|корп\.?|корпус)\s*\d+',
    re.IGNORECASE,
)

# Серия и номер паспорта — только с контекстным словом рядом.
# Без контекста \b\d{4}\s?\d{6}\b матчит бюджет "1500 000000" и артикулы.
_PASSPORT = re.compile(
    r'(?:паспорт|серия|документ|паспортные\s+данные)'
    r'[^\d]{0,20}\d{4}[\s\-]?\d{6}',
    re.IGNORECASE,
)

# Имя — только по явному контексту представления (group 1 = само имя)
_NAME_CONTEXT = re.compile(
    r'(?:меня зовут|зовите меня|я —\s*|я -\s*|мое имя|моё имя)\s*'
    r'([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2})',
    re.IGNORECASE,
)

# Порядок важен: сначала специфичные (паспорт, телефон), потом общие.
# Имя — последним: самый осторожный паттерн.
_PATTERNS: list[_Pattern] = [
    _Pattern(_PASSPORT,        "ПАСПОРТ", False),
    _Pattern(_PHONE,           "ТЕЛЕФОН", False),
    _Pattern(_EMAIL,           "EMAIL",   False),
    _Pattern(_ADDRESS_STREET,  "АДРЕС",   False),
    _Pattern(_ADDRESS_FLAT,    "АДРЕС",   False),
    _Pattern(_NAME_CONTEXT,    "ИМЯ",     True),
]


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """
    Нормализовать Unicode перед деперсонализацией.
    Заменяет все нестандартные пробелы на обычный —
    защищает от обхода паттернов через \u00a0, \u202f и т.д.
    """
    # Заменяем Unicode-пробелы на обычный пробел
    text = re.sub(r'[\u00a0\u202f\u2009\u200b\u2060\u3000\ufeff]', ' ', text)
    # NFKC нормализация — разворачивает лигатуры и fullwidth символы
    return unicodedata.normalize('NFKC', text)


def mask_for_log(value: str) -> str:
    """
    Маскировать персональные данные для безопасного вывода в лог.
    Используется в tools.py и других модулях где логируются данные клиента.

    Примеры:
        "Андрей"             → "Ан***ей"
        "+7 921 111-22-33"   → "+7***33"
        "user@mail.ru"       → "us***ru"
        "abc"                → "***"
    """
    if not value:
        return "***"
    value = str(value)
    if len(value) <= 4:
        return "***"
    return value[:2] + "***" + value[-2:]


# ── Публичный интерфейс ───────────────────────────────────────────────────────

def depersonalize(user_id: str, text: str) -> str:
    """
    Заменить ПД в тексте на пронумерованные токены.
    Маппинг токен→оригинал сохраняется в SESSION_CACHE[user_id]['pii_map'].

    Пример:
        "Меня зовут Андрей, тел. +7 921 111-22-33"
        → "Меня зовут [ИМЯ_0], тел. [ТЕЛЕФОН_0]"

    При переполнении pii_map (50 записей) выбрасывается RuntimeError,
    перехватывается и возвращается пустая строка — DeepSeek не получает ПД.
    """
    if not text or not text.strip():
        return text

    # Защита от ReDoS — обрезаем сверхдлинные сообщения
    if len(text) > MAX_MESSAGE_LEN:
        logger.warning(
            "[DEPERSONALIZE] user=%s сообщение обрезано %d→%d символов",
            hash_uid(user_id), len(text), MAX_MESSAGE_LEN,
        )
        text = text[:MAX_MESSAGE_LEN]

    # Unicode-нормализация — защита от обхода паттернов
    text = _normalize(text)

    # Загружаем сессию один раз за весь вызов
    session = get_session(user_id)
    pii_map = session["pii_map"]

    # Обратный индекс value→token для O(1) поиска дублей
    reverse: dict[str, str] = {v: k for k, v in pii_map.items()}

    try:
        for pat in _PATTERNS:
            def _replacer(match: re.Match,
                          _label: str = pat.label,
                          _is_group: bool = pat.is_group) -> str:
                try:
                    original = match.group(1) if _is_group else match.group(0)
                    if not original or not original.strip():
                        return match.group(0)

                    # Уже в кэше — используем существующий токен
                    if original in reverse:
                        token = reverse[original]
                        if _is_group:
                            return match.group(0).replace(original, token, 1)
                        return token

                    # Новый токен: max(существующих индексов) + 1 — защита от коллизий
                    prefix   = f"[{_label}_"
                    existing = [
                        int(k[len(prefix):-1]) for k in pii_map
                        if k.startswith(prefix) and k[len(prefix):-1].isdigit()
                    ]
                    n     = (max(existing) + 1) if existing else 0
                    token = f"[{_label}_{n}]"

                    if add_pii(user_id, token, original):
                        reverse[original] = token
                        logger.debug(
                            "[DEPERSONALIZE] user=%s %s обнаружен",
                            hash_uid(user_id), _label,
                        )
                    else:
                        logger.warning(
                            "[DEPERSONALIZE] pii_map переполнен user=%s, %s не заменён",
                            hash_uid(user_id), _label,
                        )
                        raise RuntimeError(
                            f"pii_map переполнен для пользователя — передача в DeepSeek заблокирована"
                        )

                    if _is_group:
                        return match.group(0).replace(original, token, 1)
                    return token

                except RuntimeError:
                    raise
                except Exception:
                    logger.error(
                        "[DEPERSONALIZE] Ошибка замены user=%s label=%s — "
                        "возвращаем оригинальный фрагмент",
                        hash_uid(user_id), _label,
                    )
                    return match.group(0)

            text = pat.regex.sub(_replacer, text)
    except RuntimeError as e:
        logger.error("[DEPERSONALIZE] user=%s %s", hash_uid(user_id), str(e))
        return ""

    return text


def personalize(user_id: str, text: str) -> str:
    """
    Восстановить оригинальные ПД из токенов.
    Вызывается после получения ответа от DeepSeek перед записью в БД.

    Пример:
        "Отлично, [ИМЯ_0]! Ваш номер [ТЕЛЕФОН_0] записал."
        → "Отлично, Андрей! Ваш номер +7 921 111-22-33 записал."
    """
    if not text or not text.strip():
        return text

    session = get_session(user_id)
    pii_map = session["pii_map"]

    if not pii_map:
        return text

    result = text
    for token, original in pii_map.items():
        pattern = re.escape(token)
        result = re.sub(pattern, lambda m: original, result, flags=re.IGNORECASE)

    return result


def depersonalize_history(user_id: str, messages: list[dict]) -> list[dict]:
    """
    Деперсонализировать историю сообщений перед отправкой в DeepSeek.

    Правила:
    — role='user'      → депер-я (клиент мог написать ПД)
    — role='system'    → не трогаем (статичный промпт без ПД)
    — role='assistant' → не трогаем (ответы бота без ПД)

    Возвращает новый список — оригинальный не изменяется.
    """
    result = []
    for msg in messages:
        if msg.get("role") == "user":
            result.append({
                **msg,
                "content": depersonalize(user_id, msg.get("content") or ""),
            })
        else:
            result.append(msg)
    return result