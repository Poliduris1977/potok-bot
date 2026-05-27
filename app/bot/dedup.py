# app/bot/dedup.py
# Дедупликация входящих webhook по update_id.
#
# Telegram повторяет вебхук если бот не ответил за ~5с (при долгом LLM).
# Без дедупликации один апдейт обрабатывается дважды → дубли сообщений.
#
# Реализация: in-memory OrderedDict с ограничением размера (LRU-like).
# При рестарте очищается — нормально, старые повторы Telegram уже не придут.

from __future__ import annotations

import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)

_MAXSIZE = 2000  # ~2-3 часа трафика при нагрузке

# platform → OrderedDict{update_id: True}
_seen: dict[str, OrderedDict] = {}


def is_duplicate(platform: str, update_id: int | str) -> bool:
    """
    Вернуть True если апдейт уже обрабатывался (дубль).
    Вернуть False и зарегистрировать если новый.
    """
    key = str(update_id)

    if platform not in _seen:
        _seen[platform] = OrderedDict()

    cache = _seen[platform]

    if key in cache:
        logger.warning(
            "[DEDUP] Дубль update_id=%s platform=%s — пропускаем",
            key, platform
        )
        return True

    cache[key] = True

    # Вытесняем старые записи при переполнении
    while len(cache) > _MAXSIZE:
        cache.popitem(last=False)

    return False