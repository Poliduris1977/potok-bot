# app/ai/gigachat.py
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

_TOKEN_URL = settings.gigachat_auth_url
_CHAT_URL  = f"{settings.gigachat_api_url}/chat/completions"
_MODEL     = "GigaChat-2-Max"

_RETRY_ATTEMPTS = 3
_RETRY_DELAYS   = [1, 2, 4]


# ── Mock ──────────────────────────────────────────────────────────────────────

def _get_mock_replies() -> list[str]:
    name = settings.bot_name
    return [
        "Расскажите подробнее — что именно вас интересует?",
        "Какой примерный размер помещения? Это поможет подобрать варианты.",
        "Есть предпочтения по стилю — классика, модерн, минимализм?",
        "Хорошо. Когда планируете сделать замер? Выезд бесплатный, ни к чему не обязывает.",
        "Скажите ваш адрес и удобное время — запишем на замер.",
        f"Всё записала! {name} свяжется с вами для подтверждения. Есть ещё вопросы?",
        "Уточните — это для квартиры или загородного дома?",
        "Подскажите примерный бюджет, чтобы предложить оптимальный вариант.",
    ]

_mock_counter: dict[str, int] = {}
_mock_fc_counter: dict[str, int] = {}


def _is_mock() -> bool:
    return settings.gigachat_client_id == "placeholder"


def _mock_reply(session_key: str) -> str:
    replies = _get_mock_replies()
    idx = _mock_counter.get(session_key, 0)
    reply = replies[idx % len(replies)]
    _mock_counter[session_key] = idx + 1
    return reply


def _mock_function_response(session_key: str, messages: list[dict]) -> dict:
    idx = _mock_fc_counter.get(session_key, 0)
    _mock_fc_counter[session_key] = idx + 1
    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    import re
    phone_match = re.search(r"(?:\+7|8)?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", last_user)
    if phone_match and idx % 2 == 0:
        return {"finish_reason": "function_call", "message": {"role": "assistant", "content": "",
            "function_call": {"name": "save_lead_data",
                "arguments": json.dumps({"phone": phone_match.group(0).strip()}, ensure_ascii=False)}}}
    return {"finish_reason": "stop", "message": {"role": "assistant", "content": _mock_reply(session_key)}}


# ── OAuth токен ───────────────────────────────────────────────────────────────

_access_token: str | None = None
_token_expires_at: float = 0.0


async def _get_access_token() -> str:
    global _access_token, _token_expires_at
    import time
    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    rq_uid = str(uuid.uuid4())
    headers = {
        "Authorization": f"Basic {_build_basic_auth()}",
        "RqUID": rq_uid,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            _TOKEN_URL, headers=headers,
            data={"scope": settings.gigachat_scope}, ssl=False
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GigaChat auth failed {resp.status}: {await resp.text()}")
            body = await resp.json()
            _access_token = body["access_token"]
            _token_expires_at = body.get("expires_at", 0) / 1000
            logger.info("GigaChat токен получен")
            return _access_token


def _build_basic_auth() -> str:
    import base64
    return base64.b64encode(
        f"{settings.gigachat_client_id}:{settings.gigachat_client_secret}".encode()
    ).decode()


def _clean_functions(functions: list[dict]) -> list[dict]:
    """
    Приводим схему functions к формату GigaChat:
    - убираем пустой required: [] (GigaChat на него ругается)
    - убираем вложенные description если они дублируются
    """
    cleaned = []
    for fn in functions:
        fn_copy = dict(fn)
        if "parameters" in fn_copy:
            params = dict(fn_copy["parameters"])
            # Убираем пустой required — GigaChat не принимает []
            if "required" in params and params["required"] == []:
                del params["required"]
            fn_copy["parameters"] = params
        cleaned.append(fn_copy)
    return cleaned


async def _request(payload: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None

    # Логируем payload для отладки (только первые 500 символов)
    payload_preview = json.dumps(payload, ensure_ascii=False)[:500]
    logger.debug("GigaChat payload (preview): %s", payload_preview)

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            token = await _get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _CHAT_URL, json=payload, headers=headers, ssl=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 401:
                        global _access_token
                        _access_token = None
                        raise RuntimeError("Token expired")
                    if resp.status != 200:
                        error_body = await resp.text()
                        logger.error("GigaChat error body: %s", error_body)
                        raise RuntimeError(f"GigaChat {resp.status}: {error_body}")
                    body = await resp.json()
                    return body["choices"][0]

        except Exception as e:
            last_error = e
            logger.warning("GigaChat попытка %d/%d: %s", attempt, _RETRY_ATTEMPTS, e)
            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAYS[attempt - 1])

    raise RuntimeError(f"GigaChat недоступен: {last_error}")


# ── Публичный API ─────────────────────────────────────────────────────────────

async def chat(
    messages: list[dict[str, str]],
    system_prompt: str,
    session_key: str = "default",
) -> str:
    if _is_mock():
        return _mock_reply(session_key)

    payload = {
        "model": _MODEL,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
        "temperature": 0.7,
        "max_tokens": 512,
    }
    try:
        choice = await _request(payload)
        return choice["message"]["content"]
    except Exception as e:
        logger.error("GigaChat chat() error: %s", e)
        return "Извините, небольшие технические неполадки. Попробуйте через минуту."


async def chat_with_functions(
    messages: list[dict[str, Any]],
    system_prompt: str,
    functions: list[dict[str, Any]],
    session_key: str = "default",
) -> dict[str, Any]:
    if _is_mock():
        return _mock_function_response(session_key, messages)

    payload = {
        "model": _MODEL,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
        "functions": _clean_functions(functions),
        # "function_call": "auto" — GigaChat не поддерживает это поле, убрали
        "temperature": 0.7,
        "max_tokens": 512,
    }
    try:
        return await _request(payload)
    except Exception as e:
        logger.error("GigaChat chat_with_functions() error: %s", e)
        return {
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "Извините, небольшие технические неполадки. Попробуйте через минуту.",
            },
        }
