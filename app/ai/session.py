# app/ai/session.py
# SESSION_CACHE — хранилище состояния диалогов в оперативной памяти.
#
# Хранит per user_id:
#   provider    — активный провайдер ("deepseek" | "gigachat")
#   pii_map     — маппинг плейсхолдер → оригинальные персональные данные
#   last_active — unix timestamp последнего обращения (обновляется автоматически)
#
# TTL:     30 минут с момента последнего обращения.
# Очистка: фоновая asyncio-задача каждые 5 минут.
#
# ⚠️  MVP: данные хранятся в оперативной памяти.
#     До продакшн-масштабирования — обязательно заменить на Redis.
#     In-memory не переживает перезапуск и не масштабируется горизонтально.

from __future__ import annotations

import asyncio
import logging
import time
from typing import TypedDict

from app.utils import hash_uid

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

SESSION_TTL      = 30 * 60   # 30 минут в секундах
CLEANUP_INTERVAL =  5 * 60   # интервал проверки — каждые 5 минут
MAX_SESSIONS     = 10_000    # предел кэша — предупреждение в лог
MAX_PII_ENTRIES  = 50        # предел записей в pii_map на одну сессию

VALID_PROVIDERS = frozenset({"deepseek", "gigachat"})

# ── Типы ──────────────────────────────────────────────────────────────────────

class Session(TypedDict):
    provider:    str              # "deepseek" | "gigachat"
    pii_map:     dict[str, str]   # плейсхолдер → оригинальное значение
    last_active: float            # unix timestamp


# ── Хранилище ─────────────────────────────────────────────────────────────────

SESSION_CACHE: dict[str, Session] = {}

# Ссылка на фоновую задачу — нужна для отмены при shutdown
_cleanup_task: asyncio.Task | None = None


# ── Публичный интерфейс ───────────────────────────────────────────────────────

def _evict_oldest() -> None:
    """
    Вытеснить одну самую старую сессию из кэша (LRU eviction).
    Вызывается когда SESSION_CACHE достиг MAX_SESSIONS.
    Защищает от бесконтрольного роста памяти при DDoS или высокой нагрузке.
    """
    if not SESSION_CACHE:
        return
    oldest_uid = min(SESSION_CACHE, key=lambda uid: SESSION_CACHE[uid].get("last_active", 0))
    if oldest_uid in SESSION_CACHE:
        SESSION_CACHE[oldest_uid]["pii_map"].clear()   # явное уничтожение ПД (152-ФЗ ст. 21)
    SESSION_CACHE.pop(oldest_uid, None)
    logger.warning("[SESSION] LRU eviction: вытеснена сессия user=%s", hash_uid(oldest_uid))


def get_session(user_id: str) -> Session:
    """
    Вернуть сессию для пользователя.
    Если сессии нет — создать с дефолтными значениями (DeepSeek, пустой pii_map).
    last_active обновляется автоматически при каждом обращении.

    При достижении MAX_SESSIONS вытесняет наиболее старую сессию (LRU)
    вместо того чтобы расти бесконечно.
    """
    if user_id not in SESSION_CACHE:
        if len(SESSION_CACHE) >= MAX_SESSIONS:
            logger.warning(
                "[SESSION] Кэш достиг предела %d сессий — вытесняем старейшую. "
                "Рассмотрите переход на Redis.",
                MAX_SESSIONS,
            )
            _evict_oldest()

        SESSION_CACHE[user_id] = Session(
            provider="deepseek",
            pii_map={},
            last_active=time.time(),
        )
    else:
        # Обновляем last_active при каждом обращении — отдельный touch не нужен
        SESSION_CACHE[user_id]["last_active"] = time.time()

    return SESSION_CACHE[user_id]


def get_session_or_none(user_id: str) -> Session | None:
    """
    Вернуть сессию без создания и без обновления last_active.
    Используется для read-only проверок (мониторинг, отладка).
    """
    return SESSION_CACHE.get(user_id)


def get_provider(user_id: str) -> str:
    """
    Вернуть активный провайдер для пользователя.
    Если сессии нет или значение некорректно — возвращает 'deepseek'.
    """
    session = SESSION_CACHE.get(user_id)
    if not session:
        return "deepseek"
    provider = session.get("provider", "deepseek")
    return provider if provider in VALID_PROVIDERS else "deepseek"


def set_provider(user_id: str, provider: str) -> None:
    """
    Переключить провайдера (вызывается при fallback на GigaChat).
    Создаёт сессию если её нет.
    """
    if provider not in VALID_PROVIDERS:
        logger.error("[SESSION] Неизвестный провайдер: %s", provider)
        return
    session = get_session(user_id)
    session["provider"] = provider
    logger.info("[SESSION] user=%s provider→%s", hash_uid(user_id), provider)


def add_pii(user_id: str, token: str, original: str) -> bool:
    """
    Добавить запись в pii_map.
    Возвращает False если достигнут лимит MAX_PII_ENTRIES.
    """
    session = get_session(user_id)
    pii_map = session["pii_map"]

    if len(pii_map) >= MAX_PII_ENTRIES:
        logger.warning(
            "[SESSION] pii_map user=%s достиг предела %d записей",
            hash_uid(user_id), MAX_PII_ENTRIES,
        )
        return False

    pii_map[token] = original
    return True


def reset_session(user_id: str) -> None:
    """Полностью сбросить сессию (например, при команде /start)."""
    if user_id in SESSION_CACHE:
        SESSION_CACHE[user_id]["pii_map"].clear()   # явное уничтожение ПД (152-ФЗ ст. 21)
    SESSION_CACHE.pop(user_id, None)
    logger.debug("[SESSION] Сессия сброшена: user=%s", hash_uid(user_id))


def session_stats() -> dict:
    """
    Статистика кэша для health-check и мониторинга.
    Не обновляет last_active.
    """
    sessions = list(SESSION_CACHE.values())
    now      = time.time()
    return {
        "total":    len(sessions),
        "deepseek": sum(1 for s in sessions if s.get("provider") == "deepseek"),
        "gigachat": sum(1 for s in sessions if s.get("provider") == "gigachat"),
        "expiring": sum(                                   # >24 мин неактивности
            1 for s in sessions
            if now - s.get("last_active", 0) > SESSION_TTL * 0.8
        ),
        "capacity": MAX_SESSIONS,
    }


# ── Фоновая очистка ───────────────────────────────────────────────────────────

async def cleanup_sessions() -> None:
    """
    Фоновая asyncio-задача. Удаляет сессии старше SESSION_TTL.
    Запускается через start_cleanup() в lifespan main.py.
    Корректно завершается при отмене задачи (CancelledError).
    """
    logger.info(
        "[SESSION] Фоновая очистка запущена (TTL=%ds, интервал=%ds)",
        SESSION_TTL, CLEANUP_INTERVAL,
    )
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                now     = time.time()
                expired = [
                    uid for uid, data in list(SESSION_CACHE.items())
                    if now - data.get("last_active", 0) > SESSION_TTL
                ]
                for uid in expired:
                    if uid in SESSION_CACHE:
                        SESSION_CACHE[uid]["pii_map"].clear()  # явное уничтожение ПД (152-ФЗ ст. 21)
                    SESSION_CACHE.pop(uid, None)
                if expired:
                    logger.info("[SESSION] Очищено %d сессий", len(expired))
            except Exception as e:
                # Используем error вместо exception — полный трейсбек может
                # содержать pii_map с персональными данными клиентов
                logger.error(
                    "[SESSION] Ошибка в цикле очистки: %s", type(e).__name__
                )

    except asyncio.CancelledError:
        logger.info("[SESSION] Фоновая очистка остановлена (shutdown)")
        raise  # пробрасываем — asyncio ожидает CancelledError при отмене задачи


def start_cleanup() -> None:
    """
    Запустить фоновую задачу очистки и сохранить ссылку для отмены.
    Вызывать в lifespan main.py при старте приложения.
    """
    global _cleanup_task
    _cleanup_task = asyncio.create_task(cleanup_sessions())
    logger.info("[SESSION] Задача очистки создана")


def stop_cleanup() -> None:
    """
    Отменить фоновую задачу очистки.
    Вызывать в lifespan main.py при остановке приложения.
    """
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        logger.info("[SESSION] Задача очистки отменена")
    _cleanup_task = None