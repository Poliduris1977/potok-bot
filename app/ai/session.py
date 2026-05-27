# app/ai/session.py
# SESSION_CACHE — хранилище состояния диалогов в Redis.
#
# Хранит per user_id (JSON, TTL 30 минут):
#   provider    — активный провайдер ("deepseek" | "gigachat")
#   pii_map     — маппинг плейсхолдер → оригинальные персональные данные
#
# TTL сбрасывается при каждом обращении (скользящее окно).
# Очистка: Redis сам удаляет ключи по TTL — start_cleanup/stop_cleanup — no-op.
#
# Ключ в Redis: session:{user_id}

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TypedDict

import redis.asyncio as aioredis

from app.config import settings
from app.utils import hash_uid

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

SESSION_TTL     = 30 * 60   # 30 минут в секундах
MAX_PII_ENTRIES = 50        # предел записей в pii_map на одну сессию

VALID_PROVIDERS = frozenset({"deepseek", "gigachat"})

# ── Типы ──────────────────────────────────────────────────────────────────────

class Session(TypedDict):
    provider: str            # "deepseek" | "gigachat"
    pii_map:  dict[str, str] # плейсхолдер → оригинальное значение


# ── Redis клиент ──────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


async def close_redis() -> None:
    """Закрыть соединение с Redis. Вызывать в lifespan при shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info("[SESSION] Redis соединение закрыто")


# ── Внутренние хелперы ────────────────────────────────────────────────────────

def _key(user_id: str) -> str:
    return f"session:{user_id}"


async def _load(user_id: str) -> Session | None:
    """Загрузить сессию из Redis. None если не существует."""
    try:
        raw = await _get_redis().get(_key(user_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return Session(
            provider=data.get("provider", "deepseek"),
            pii_map=data.get("pii_map", {}),
        )
    except Exception as e:
        logger.error("[SESSION] Ошибка чтения из Redis: %s", type(e).__name__)
        return None


async def _save(user_id: str, session: Session) -> None:
    """Сохранить сессию в Redis с обновлением TTL."""
    try:
        await _get_redis().setex(
            _key(user_id),
            SESSION_TTL,
            json.dumps({"provider": session["provider"], "pii_map": session["pii_map"]}),
        )
    except Exception as e:
        logger.error("[SESSION] Ошибка записи в Redis: %s", type(e).__name__)


async def _default_session() -> Session:
    return Session(provider="deepseek", pii_map={})


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def get_session(user_id: str) -> Session:
    """
    Вернуть сессию для пользователя.
    Если сессии нет — создать с дефолтными значениями.
    TTL обновляется при каждом обращении.
    """
    session = await _load(user_id)
    if session is None:
        session = await _default_session()
    await _save(user_id, session)  # обновляем TTL
    return session


async def get_provider(user_id: str) -> str:
    """Вернуть активный провайдер. Если нет сессии — 'deepseek'."""
    session = await _load(user_id)
    if not session:
        return "deepseek"
    provider = session.get("provider", "deepseek")
    return provider if provider in VALID_PROVIDERS else "deepseek"


async def set_provider(user_id: str, provider: str) -> None:
    """Переключить провайдера (вызывается при fallback на GigaChat)."""
    if provider not in VALID_PROVIDERS:
        logger.error("[SESSION] Неизвестный провайдер: %s", provider)
        return
    session = await get_session(user_id)
    session["provider"] = provider
    await _save(user_id, session)
    logger.info("[SESSION] user=%s provider→%s", hash_uid(user_id), provider)


async def add_pii(user_id: str, token: str, original: str) -> bool:
    """
    Добавить запись в pii_map.
    Возвращает False если достигнут лимит MAX_PII_ENTRIES.
    """
    session = await get_session(user_id)
    pii_map = session["pii_map"]

    if len(pii_map) >= MAX_PII_ENTRIES:
        logger.warning(
            "[SESSION] pii_map user=%s достиг предела %d записей",
            hash_uid(user_id), MAX_PII_ENTRIES,
        )
        return False

    pii_map[token] = original
    await _save(user_id, session)
    return True


async def reset_session(user_id: str) -> None:
    """Полностью сбросить сессию (например, при команде /start)."""
    try:
        # Явное уничтожение ПД перед удалением ключа (152-ФЗ ст. 21)
        session = await _load(user_id)
        if session:
            session["pii_map"].clear()
            await _save(user_id, session)
        await _get_redis().delete(_key(user_id))
        logger.debug("[SESSION] Сессия сброшена: user=%s", hash_uid(user_id))
    except Exception as e:
        logger.error("[SESSION] Ошибка сброса сессии: %s", type(e).__name__)


async def get_session_or_none(user_id: str) -> Session | None:
    """Вернуть сессию без создания и без обновления TTL."""
    return await _load(user_id)


# ── Совместимость с lifespan ──────────────────────────────────────────────────
# start_cleanup / stop_cleanup — no-op: Redis сам удаляет ключи по TTL.
# Оставлены чтобы не менять main.py.

def start_cleanup() -> None:
    logger.info("[SESSION] Redis-режим: TTL-очистка на стороне Redis, задача не нужна")


def stop_cleanup() -> None:
    pass