# app/database.py
# Управление пулом asyncpg-соединений.
# Пул создаётся при старте FastAPI (lifespan) и закрывается при остановке.
# Все модули получают соединение через get_db().

from __future__ import annotations

import asyncpg
from asyncpg import Pool, Connection
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from app.config import settings

_pool: Pool | None = None


async def create_pool() -> None:
    """Создать пул соединений. Вызывается один раз в lifespan."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        max_inactive_connection_lifetime=300.0,
    )


async def close_pool() -> None:
    """Закрыть пул при остановке приложения."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized. Call create_pool() first.")
    return _pool


@asynccontextmanager
async def get_db() -> AsyncGenerator[Connection, None]:
    """
    Контекстный менеджер для получения соединения из пула.

    Использование:
        async with get_db() as conn:
            row = await conn.fetchrow("SELECT ...")
    """
    async with get_pool().acquire(timeout=5.0) as connection:
        yield connection