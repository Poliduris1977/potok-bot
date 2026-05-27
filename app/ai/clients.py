# app/ai/clients.py
# Синглтоны AI-клиентов: DeepSeek V4 Pro (основной) и GigaChat (fallback).
#
# DeepSeek — через OpenRouter API (OpenAI-совместимый).
# GigaChat  — обёртка над существующим gigachat.py (OAuth2, авторефреш токена).
#
# Оба клиента возвращают единый формат ответа, который ждёт agent.py:
#   {
#       "finish_reason": "stop" | "tool_calls" | "function_call",
#       "message": {
#           "content":    "...",
#           "tool_calls": [...],   # только при finish_reason="tool_calls"
#       }
#   }
#
# Инициализация: вызвать init_clients() в lifespan main.py.
# Доступ:        deepseek_client и gigachat_client как модульные синглтоны.

from __future__ import annotations

import logging
import re
import time

from openai import AsyncOpenAI

from app.config import settings
from app.utils import hash_uid

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_MODEL           = "deepseek/deepseek-v4-pro"
_TIMEOUT_DS      = 60.0   # секунд — с запасом под reasoning xhigh
_TIMEOUT_GC      = 30.0   # секунд — GigaChat без thinking


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _functions_to_tools(functions: list[dict]) -> list[dict]:
    """Конвертировать список функций в формат OpenAI tools."""
    return [{"type": "function", "function": fn} for fn in functions]


def _strip_thinking(content: str | None) -> str:
    """Удалить блок <think>...</think> из ответа DeepSeek."""
    if not content:
        return ""
    # Быстрая проверка до regex — большинство ответов без thinking
    if "<think>" not in content:
        return content
    return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()


def _normalize_openai_response(choice) -> dict:
    """
    Привести ответ OpenAI SDK к единому формату agent.py.
    Обрабатывает: text, tool_calls, пустой ответ.
    """
    finish_reason = choice.finish_reason or "stop"
    message       = choice.message

    result: dict = {
        "finish_reason": finish_reason,
        "message": {
            "content": _strip_thinking(message.content),
        },
    }

    if finish_reason == "tool_calls" and message.tool_calls:
        result["message"]["tool_calls"] = [
            {
                "id":   tc.id,
                "type": "function",
                "function": {
                    "name":      tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in message.tool_calls
        ]

    return result


def _get_clients() -> tuple["DeepSeekClient", "GigaChatClient"]:
    """
    Проверить что клиенты инициализированы и вернуть их.
    Выбрасывает RuntimeError с понятным сообщением если init_clients() не вызван.
    """
    if deepseek_client is None or gigachat_client is None:
        raise RuntimeError(
            "AI-клиенты не инициализированы. "
            "Убедитесь что init_clients() вызван в lifespan main.py."
        )
    return deepseek_client, gigachat_client


# ── DeepSeekClient ────────────────────────────────────────────────────────────

class DeepSeekClient:
    """
    Клиент DeepSeek V4 Pro через OpenRouter.
    Ленивая инициализация AsyncOpenAI — создаётся при первом запросе,
    не в __init__, чтобы не конфликтовать с event loop.
    """

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=_OPENROUTER_BASE,
                api_key=settings.openrouter_api_key,
                default_headers={
                    "X-Or-Zdr":       "true",        # Zero Data Retention
                    "HTTP-Referer":   settings.site_url_str,
                    "X-Title":        "POTOK",        # ASCII only
                },
            )
        return self._client

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str,
        functions: list[dict],
        session_key: str = "default",
    ) -> dict:
        """
        Отправить запрос в DeepSeek V4 Pro через OpenRouter.
        Возвращает нормализованный dict в формате agent.py.
        """
        t0 = time.monotonic()

        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        tools         = _functions_to_tools(functions)

        client   = self._get_client()
        response = await client.chat.completions.create(
            model=_MODEL,
            messages=full_messages,
            tools=tools,
            # tool_choice не передаём — DeepSeek сам решает когда вызывать
            extra_body={"reasoning": {"effort": "xhigh"}},
            timeout=_TIMEOUT_DS,
        )

        elapsed = time.monotonic() - t0

        if not response.choices:
            raise ValueError("DeepSeek вернул пустой список choices")

        result = _normalize_openai_response(response.choices[0])

        # Логируем без ПД — только метаданные
        usage = response.usage
        details    = getattr(usage, "prompt_tokens_details", None)
        cache_hits = getattr(details, "cached_tokens", 0)
        logger.info(
            "[DEEPSEEK] session=%s finish=%s elapsed=%.1fs "
            "tokens=in:%s/out:%s cache_hit:%s",
            hash_uid(session_key),
            result["finish_reason"],
            elapsed,
            getattr(usage, "prompt_tokens", "?"),
            getattr(usage, "completion_tokens", "?"),
            cache_hits,
        )

        return result


# ── GigaChatClient ────────────────────────────────────────────────────────────

class GigaChatClient:
    """
    Обёртка над существующим gigachat.py.
    Авторефреш OAuth2-токена — в gigachat.py.
    Возвращает тот же формат что и DeepSeekClient.
    """

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str,
        functions: list[dict],
        session_key: str = "default",
    ) -> dict:
        """
        Отправить запрос в GigaChat-2-Max.
        Возвращает нормализованный dict в формате agent.py.
        """
        from app.ai.gigachat import chat_with_functions

        t0     = time.monotonic()
        result = await chat_with_functions(
            messages=messages,
            system_prompt=system_prompt,
            functions=functions,
            session_key=session_key,
        )
        elapsed = time.monotonic() - t0

        logger.info(
            "[GIGACHAT] session=%s finish=%s elapsed=%.1fs",
            hash_uid(session_key),
            result.get("finish_reason", "?"),
            elapsed,
        )

        return result


# ── Синглтоны ─────────────────────────────────────────────────────────────────

deepseek_client: DeepSeekClient | None = None
gigachat_client: GigaChatClient | None = None


def init_clients() -> None:
    """
    Инициализировать синглтоны клиентов.
    Вызывать в lifespan main.py при старте приложения.
    AsyncOpenAI создаётся лениво при первом запросе — event loop уже активен.
    """
    global deepseek_client, gigachat_client
    deepseek_client = DeepSeekClient()
    gigachat_client = GigaChatClient()
    logger.info(
        "[CLIENTS] Инициализированы: DeepSeek(%s) + GigaChat(fallback)",
        _MODEL,
    )