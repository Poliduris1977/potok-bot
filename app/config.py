# app/config.py
#
# Конфигурация приложения через переменные окружения / .env файл.
#
# Безопасность:
#   — Критичные ключи валидируются при старте через model_validator
#   — При ошибке поднимается RuntimeError (не sys.exit) — процесс завершается в lifespan
#   — GigaChat URL проверяется на HTTPS
#   — rate_limit_per_minute, admin_secret — ограничения на длину/диапазон
#   — webhook_secret и admin_secret требуют минимум 32 символа
#   — environment валидируется через Literal — is_production не ломается при опечатке
#   — site_url хранится как str — AnyHttpUrl добавляет trailing slash в Pydantic v2
#
# Тесты:
#   get_settings.cache_clear() перед тестом + monkeypatch переменных окружения.
#   RuntimeError вместо sys.exit — тест поймает исключение, не завершится.

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",   # продакшн: поменять на "forbid" чтобы ловить опечатки
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token:      str = ""
    telegram_webhook_secret: str = ""   # для верификации подписи входящих webhook

    # ── MAX ───────────────────────────────────────────────────────────────────
    max_bot_token:      str = ""
    max_webhook_secret: str = ""        # для верификации подписи входящих webhook

    # ── GigaChat (fallback-провайдер) ─────────────────────────────────────────
    gigachat_client_id:     str = ""
    gigachat_client_secret: str = ""
    gigachat_scope:    str = "GIGACHAT_API_PERS"
    gigachat_auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_api_url:  str = "https://gigachat.devices.sberbank.ru/api/v1"

    # ── OpenRouter (основной AI-провайдер) ────────────────────────────────────
    openrouter_api_key: str = ""
    # str вместо AnyHttpUrl — Pydantic v2 добавляет trailing slash к AnyHttpUrl,
    # что может не совпасть с зарегистрированным Referer в OpenRouter dashboard
    site_url: str = "https://potok-bot.ru"

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = ""

    # ── Уведомления ───────────────────────────────────────────────────────────
    director_chat_id: Optional[int] = None
    director_max_id:  str = ""
    company_phone:    str = ""

    # ── Deploy ────────────────────────────────────────────────────────────────
    webhook_base_url: str = "http://localhost:8000"
    # Literal вместо str — is_production не ломается при опечатке "Production"
    environment: Literal["development", "staging", "production"] = "development"
    # Отдельный секрет для /admin/* — разный уровень доступа от webhook_secret
    admin_secret: str = ""

    # ── Bot persona ───────────────────────────────────────────────────────────
    bot_name:     str                       = "Игорь"
    bot_gender:   Literal["male", "female"] = "male"
    company_name: str                       = "КухниПро"

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Макс. сообщений от одного user_id в минуту",
    )

    # ── Размер тела запроса ───────────────────────────────────────────────────
    max_request_body_kb: int = Field(
        default=64,
        ge=1,
        le=1024,
        description="Макс. размер тела входящего запроса в КБ",
    )

    # ── Валидация при старте ──────────────────────────────────────────────────

    @model_validator(mode="after")
    def validate_critical(self) -> "Settings":
        """
        Проверяет критичные поля конфигурации при старте.
        Поднимает RuntimeError — lifespan в main.py логирует и завершает процесс.
        RuntimeError (не sys.exit) позволяет тестам поймать исключение.
        """
        errors:   list[str] = []
        warnings: list[str] = []

        # ── Обязательные поля ────────────────────────────────────────────────
        if not self.openrouter_api_key:
            errors.append(
                "OPENROUTER_API_KEY не задан — "
                "получите ключ на https://openrouter.ai/keys"
            )

        if not self.telegram_bot_token:
            errors.append(
                "TELEGRAM_BOT_TOKEN не задан — "
                "получите токен у @BotFather в Telegram"
            )

        if not self.database_url:
            errors.append("DATABASE_URL не задан")

        # ── Secrets: минимум 32 символа ──────────────────────────────────────
        _MIN_SECRET = 32

        if self.telegram_webhook_secret and len(self.telegram_webhook_secret) < _MIN_SECRET:
            errors.append(
                f"TELEGRAM_WEBHOOK_SECRET слишком короткий (минимум {_MIN_SECRET} символов). "
                "Сгенерируйте: python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        if self.max_webhook_secret and len(self.max_webhook_secret) < _MIN_SECRET:
            errors.append(
                f"MAX_WEBHOOK_SECRET слишком короткий (минимум {_MIN_SECRET} символов)."
            )

        if self.admin_secret and len(self.admin_secret) < _MIN_SECRET:
            errors.append(
                f"ADMIN_SECRET слишком короткий (минимум {_MIN_SECRET} символов). "
                "Сгенерируйте: python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        # ── GigaChat URL — только HTTPS ───────────────────────────────────────
        if not self.gigachat_auth_url.startswith("https://"):
            errors.append(
                "GIGACHAT_AUTH_URL должен начинаться с https://. "
                f"Текущее значение не соответствует требованиям."
            )

        if not self.gigachat_api_url.startswith("https://"):
            errors.append(
                "GIGACHAT_API_URL должен начинаться с https://. "
                f"Текущее значение не соответствует требованиям."
            )

        # ── site_url — только HTTPS ───────────────────────────────────────────
        if self.site_url and not self.site_url.startswith("https://"):
            errors.append("SITE_URL должен начинаться с https://")

        # ── webhook_base_url — только HTTPS в продакшне ───────────────────────
        if self.is_production and not self.webhook_base_url.startswith("https://"):
            errors.append(
                "WEBHOOK_BASE_URL должен начинаться с https:// в production. "
                f"Текущее значение: {self.webhook_base_url}. "
                "Telegram не принимает webhook на HTTP-адреса."
            )

        # ── Предупреждения ────────────────────────────────────────────────────
        if self.is_production and not self.gigachat_available:
            warnings.append(
                "GIGACHAT_CLIENT_ID / GIGACHAT_CLIENT_SECRET не заданы — "
                "fallback недоступен, при сбое DeepSeek бот замолчит"
            )

        if not self.director_chat_id and not self.director_max_id:
            warnings.append(
                "DIRECTOR_CHAT_ID и DIRECTOR_MAX_ID не заданы — "
                "уведомления директору отключены"
            )

        if self.is_production and not self.telegram_webhook_secret:
            warnings.append(
                "TELEGRAM_WEBHOOK_SECRET не задан — webhook Telegram не защищён"
            )

        if self.is_production and not self.admin_secret:
            warnings.append(
                "ADMIN_SECRET не задан — admin-эндпоинты недоступны"
            )

        # ── Вывод ────────────────────────────────────────────────────────────
        for w in warnings:
            logger.warning("[CONFIG] %s", w)

        if errors:
            for e in errors:
                logger.critical("[CONFIG] %s", e)
            # RuntimeError — lifespan поймает и завершит процесс корректно.
            # Тесты тоже могут поймать — в отличие от sys.exit().
            raise RuntimeError(
                f"Ошибки конфигурации ({len(errors)} шт.) — подробности в логе выше."
            )

        return self

    # ── Свойства ──────────────────────────────────────────────────────────────

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.webhook_base_url}/webhook/telegram"

    @property
    def max_webhook_url(self) -> str:
        return f"{self.webhook_base_url}/webhook/max"

    @property
    def gigachat_available(self) -> bool:
        return bool(self.gigachat_client_id and self.gigachat_client_secret)

    @property
    def site_url_str(self) -> str:
        """site_url как строка для HTTP-заголовков (без trailing slash)."""
        return self.site_url.rstrip("/")

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_request_body_kb * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()