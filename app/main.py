# app/main.py
# Точка входа FastAPI.
#
# Безопасность:
#   — logging настраивается ДО импорта других модулей проекта
#   — Webhook Telegram верифицируется через X-Telegram-Bot-Api-Secret-Token
#   — Webhook MAX верифицируется через X-Max-Webhook-Secret
#   — Неверная подпись → 404 (не 403) — скрываем факт существования эндпоинта
#   — Admin-эндпоинты защищены отдельным ADMIN_SECRET
#   — setWebhook через POST с JSON — secret_token не попадает в URL/логи
#   — MAX access_token в URL — ограничение MAX API, задокументировано ниже
#   — Rate limiting per user_id + fallback на IP если user_id недоступен
#   — Ограничение размера тела запроса через middleware (чтение stream, поддержка chunked)
#   — JSONDecodeError обрабатывается явно — 400 вместо 422
#   — UnicodeEncodeError в _verify_secret обрабатывается явно
#   — lifespan: пул БД закрывается в finally — нет утечки ресурсов при сбое
#   — Telegram error не раскрывается клиенту — только в лог
#   — Дедупликация webhook по update_id — защита от дублей при медленном LLM

from __future__ import annotations

# ── Logging настраивается первым — до импорта модулей проекта ────────────────
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

import asyncio
import hmac
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import create_pool, close_pool
from app.bot.dedup import is_duplicate


# ── Rate limiter (in-memory, sliding window) ──────────────────────────────────

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW  = 60.0


def _check_rate_limit(key: str) -> bool:
    now    = time.monotonic()
    bucket = [t for t in _rate_buckets[key] if now - t < _RATE_WINDOW]

    if len(bucket) >= settings.rate_limit_per_minute:
        _rate_buckets[key] = bucket
        return False

    bucket.append(now)
    _rate_buckets[key] = bucket
    return True


async def _cleanup_rate_buckets() -> None:
    logger.info("[RATE] Фоновая очистка запущена")
    try:
        while True:
            await asyncio.sleep(300)
            try:
                now   = time.monotonic()
                stale = [
                    k for k, b in list(_rate_buckets.items())
                    if all(now - t >= _RATE_WINDOW for t in b)
                ]
                for k in stale:
                    _rate_buckets.pop(k, None)
                if stale:
                    logger.debug("[RATE] Очищено %d устаревших ключей", len(stale))
            except Exception as e:
                logger.error("[RATE] Ошибка очистки: %s", type(e).__name__)
    except asyncio.CancelledError:
        logger.info("[RATE] Фоновая очистка остановлена (shutdown)")
        raise


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_key(request: Request, platform: str, user_id: str) -> str:
    if user_id:
        return f"{platform}:{user_id}"
    return f"ip:{_client_ip(request)}"


def _verify_secret(actual: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(actual.encode(), expected.encode())
    except (UnicodeEncodeError, AttributeError):
        return False


def _check_admin(x_admin_secret: str) -> None:
    if not settings.admin_secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET не настроен на сервере.",
        )
    if not _verify_secret(x_admin_secret, settings.admin_secret):
        raise HTTPException(status_code=404, detail="Forbidden")


# ── Lifespan ──────────────────────────────────────────────────────────────────

_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    _background_tasks.clear()

    logger.info("🚀 ПОТОК-бот запускается...")

    pool_created = False
    try:
        await create_pool()
        pool_created = True
        logger.info("✅ Пул БД создан")

        from app.ai.clients import init_clients
        init_clients()
        logger.info("✅ AI-клиенты инициализированы")

    except RuntimeError as e:
        logger.critical("[LIFESPAN] Ошибка конфигурации: %s", e)
        if pool_created:
            await close_pool()
        raise SystemExit(1)

    except Exception as e:
        logger.critical("[LIFESPAN] Ошибка при старте: %s", type(e).__name__)
        if pool_created:
            await close_pool()
        raise

    from app.ai.session import start_cleanup
    start_cleanup()
    _background_tasks.append(asyncio.create_task(_cleanup_rate_buckets()))
    logger.info("✅ Фоновые задачи запущены")

    yield

    logger.info("🛑 ПОТОК-бот останавливается...")

    from app.ai.session import stop_cleanup
    stop_cleanup()

    for task in _background_tasks:
        if not task.done():
            task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)

    await close_pool()
    logger.info("✅ ПОТОК-бот остановлен")


# ── Приложение ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="ПОТОК — бот-консультант",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
)


# ── Middleware: ограничение размера тела запроса ──────────────────────────────

@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    max_bytes = settings.max_request_body_bytes
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            logger.warning(
                "[MIDDLEWARE] Тело запроса превышает лимит %d байт, IP=%s",
                max_bytes, _client_ip(request),
            )
            return Response(status_code=413, content="Request body too large")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request._receive = receive
    request._body = body

    return await call_next(request)


# ── Health-check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "bot": settings.bot_name}


# ── Telegram webhook ──────────────────────────────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    if settings.telegram_webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not _verify_secret(token, settings.telegram_webhook_secret):
            logger.warning("[WEBHOOK] Telegram: неверная подпись, IP=%s", _client_ip(request))
            return Response(status_code=404)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("[WEBHOOK] Telegram: невалидный JSON, IP=%s", _client_ip(request))
        return Response(status_code=400)

    # Дедупликация — Telegram повторяет вебхук если бот не ответил за ~5с
    update_id = body.get("update_id")
    if update_id and is_duplicate("telegram", update_id):
        return Response(status_code=200)  # 200 — Telegram не будет повторять

    user_id = str(
        body.get("message", {}).get("from", {}).get("id") or
        body.get("callback_query", {}).get("from", {}).get("id") or ""
    )[:64]

    rl_key = _rate_key(request, "tg", user_id)
    if not _check_rate_limit(rl_key):
        logger.warning("[RATE] Telegram key=%s превысил лимит %d/мин",
                       rl_key, settings.rate_limit_per_minute)
        return Response(status_code=200)

    from app.bot.telegram import handle_update
    await handle_update(body)
    return Response(status_code=200)


# ── MAX webhook ───────────────────────────────────────────────────────────────

@app.post("/webhook/max")
async def max_webhook(request: Request) -> Response:
    if settings.max_webhook_secret:
        token = request.headers.get("X-Max-Webhook-Secret", "")
        if not _verify_secret(token, settings.max_webhook_secret):
            logger.warning("[WEBHOOK] MAX: неверная подпись, IP=%s", _client_ip(request))
            return Response(status_code=404)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("[WEBHOOK] MAX: невалидный JSON, IP=%s", _client_ip(request))
        return Response(status_code=400)

    # Дедупликация — update_id на верхнем уровне или message.id
    update_id = body.get("update_id") or body.get("message", {}).get("id")
    if update_id and is_duplicate("max", update_id):
        return Response(status_code=200)

    user_id = str(
        body.get("message", {}).get("sender", {}).get("user_id") or ""
    )[:64]

    rl_key = _rate_key(request, "max", user_id)
    if not _check_rate_limit(rl_key):
        logger.warning("[RATE] MAX key=%s превысил лимит %d/мин",
                       rl_key, settings.rate_limit_per_minute)
        return Response(status_code=200)

    from app.bot.max import handle_update as max_handle_update
    await max_handle_update(body)
    return Response(status_code=200)


# ── Admin: установка webhook'ов ───────────────────────────────────────────────

@app.post("/admin/set_webhook/telegram")
async def set_telegram_webhook(
    x_admin_secret: str = Header(default=""),
) -> JSONResponse:
    _check_admin(x_admin_secret)

    import aiohttp

    tg_api  = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    payload: dict[str, Any] = {"url": settings.telegram_webhook_url}
    if settings.telegram_webhook_secret:
        payload["secret_token"] = settings.telegram_webhook_secret

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{tg_api}/setWebhook",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if data.get("ok"):
                logger.info("Telegram webhook установлен: %s", settings.telegram_webhook_url)
                return JSONResponse({"ok": True, "webhook": settings.telegram_webhook_url})
            logger.error("[ADMIN] Telegram setWebhook error: %s", data)
            raise HTTPException(status_code=500, detail="Не удалось установить webhook")


@app.post("/admin/set_webhook/max")
async def set_max_webhook(
    x_admin_secret: str = Header(default=""),
) -> JSONResponse:
    _check_admin(x_admin_secret)

    import aiohttp

    url = f"https://platform-api.max.ru/subscriptions?access_token={settings.max_bot_token}"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json={"url": settings.max_webhook_url, "version": "1.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                logger.error("[ADMIN] MAX webhook error: status=%s body=%s",
                             resp.status, str(data)[:200])
                raise HTTPException(status_code=500, detail="Не удалось установить webhook MAX")
            logger.info("MAX webhook установлен: %s", settings.max_webhook_url)
            return JSONResponse({"ok": True, "webhook": settings.max_webhook_url})


# ── Admin: удаление лида (152-ФЗ, право на забвение) ─────────────────────────

@app.delete("/admin/lead/{lead_id}")
async def admin_delete_lead(
    lead_id: int,
    x_admin_secret: str = Header(default=""),
) -> JSONResponse:
    """
    Каскадно удалить лид и все связанные данные (152-ФЗ, право на забвение).
    Удаляет: messages → dialogs → appointments → lead.

    curl -X DELETE https://bot.prostorlab.ru/admin/lead/42 \\
         -H "X-Admin-Secret: ваш_секрет"
    """
    _check_admin(x_admin_secret)

    from app.leads.repository import delete_lead_cascade
    from app.database import get_pool

    deleted = await delete_lead_cascade(get_pool(), lead_id)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Лид {lead_id} не найден")

    logger.info("[ADMIN] Лид %d удалён (152-ФЗ)", lead_id)
    return JSONResponse({"deleted": True, "lead_id": lead_id})