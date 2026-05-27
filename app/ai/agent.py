# app/ai/agent.py
# Агентный цикл обработки диалогов.
#
# Поддерживаемые форматы Function Calling:
#   Option A — GigaChat-2-Max structured: finish_reason="function_call"
#   Option B — Старый GigaChat text-based: func_name({...}) в content
#   Option C — Обычный текстовый ответ
#   Option D — DeepSeek tool_calls: finish_reason="tool_calls", message["tool_calls"]
#
# Безопасность:
#   — Деперсонализация user-сообщений только для DeepSeek (в _call_provider)
#   — Контекст клиента [КОНТЕКСТ КЛИЕНТА] — user-сообщение, деперсонализируется авто
#   — Персонализация ответа только для DeepSeek-ответов (_maybe_personalize)
#   — При падении обоих провайдеров — уведомление директору + fallback-ответ
#   — В логах нет ПД — только метаданные

from __future__ import annotations

import asyncio
import json
import logging

from app.models import Lead, ChecklistProgress
from app.config import settings
from app.ai.prompt import build_system_prompt, format_known_info
from app.ai.tools import FUNCTIONS, AgentContext, AgentResult, dispatch
from app.ai.session import get_provider, set_provider
from app.ai.depersonalize import depersonalize_history, personalize

logger = logging.getLogger(__name__)

_MAX_ITERATIONS   = 5
_AGENT_TIMEOUT    = 65.0    # секунд — Telegram закрывает соединение ~75с, даём запас

KNOWN_FUNCTIONS = {fn["name"] for fn in FUNCTIONS}


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _parse_function_call(content: str) -> tuple:
    """
    Распарсить текстовый FC от старого GigaChat. Option B.
    Использует сбалансированный поиск скобок для поддержки вложенного JSON.
    Возвращает (func_name, args_raw, remaining) или (None, None, content).
    """
    content = content.strip()
    n = len(content)
    i = 0

    # Пропускаем ведущие пробелы
    while i < n and content[i].isspace():
        i += 1
    if i >= n:
        return None, None, content

    # Собираем имя функции
    start_name = i
    while i < n and (content[i].isalnum() or content[i] == '_'):
        i += 1
    if i == start_name:
        return None, None, content
    func_name = content[start_name:i]

    if func_name not in KNOWN_FUNCTIONS:
        return None, None, content

    # Пропускаем пробелы после имени
    while i < n and content[i].isspace():
        i += 1
    if i >= n or content[i] != '(':
        return None, None, content
    i += 1  # пропускаем '('

    # Пропускаем пробелы после '('
    while i < n and content[i].isspace():
        i += 1
    if i >= n or content[i] != '{':
        return None, None, content

    # Ищем сбалансированную закрывающую скобку JSON
    brace_count = 0
    in_string = False
    escape = False
    j = i
    while j < n:
        ch = content[j]
        if escape:
            escape = False
            j += 1
            continue
        if ch == '\\':
            escape = True
            j += 1
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            j += 1
            continue
        if not in_string:
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_end = j + 1  # включая '}'
                    break
        j += 1
    else:
        # закрывающая скобка не найдена
        return None, None, content

    # После JSON должна быть ')'
    k = json_end
    while k < n and content[k].isspace():
        k += 1
    if k >= n or content[k] != ')':
        return None, None, content

    args_raw = content[i:json_end]      # от первой '{' до последней '}'
    remaining = content[k + 1:].strip() # всё после ')'
    return func_name, args_raw, remaining


def _fallback_reply() -> str:
    """Ответ клиенту при падении обоих провайдеров."""
    phone = settings.company_phone
    if phone:
        return (
            f"Что-то пошло не так на моей стороне — напишите через минуту "
            f"или позвоните нам напрямую: {phone}. Менеджер ответит сразу."
        )
    return (
        "Что-то пошло не так на моей стороне. "
        "Напишите через минуту — я буду на связи."
    )


async def _maybe_personalize(session_key: str, text: str) -> str:
    """
    Восстановить оригинальные ПД из токенов — только для DeepSeek-ответов.
    GigaChat получает оригинальные данные, токенов в ответе быть не может.
    """
    if await get_provider(session_key) == "deepseek":
        return await personalize(session_key, text)
    return text


def _build_context_message(lead: Lead, progress: ChecklistProgress) -> list[dict]:
    """
    Построить пару [user: контекст клиента / assistant: подтверждение].
    Добавляется в начало working_messages если есть данные о клиенте.

    Используется как user-сообщение чтобы depersonalize_history()
    автоматически деперсонализировал его перед отправкой в DeepSeek.
    """
    if not lead or progress.filled_count == 0:
        return []
    known_info = format_known_info(lead, progress)
    return [
        {"role": "user",      "content": f"[КОНТЕКСТ КЛИЕНТА]\n{known_info}"},
        {"role": "assistant", "content": "Контекст принят."},
    ]


def _rebuild_messages(
    context: list[dict],
    history: list[dict],
    fc_trail: list[dict],
) -> list[dict]:
    """
    Собрать working_messages из трёх независимых частей.
    Разделение позволяет обновлять контекст после FC без потери fc_trail.

        context  — актуальные данные клиента (обновляется после каждого FC)
        history  — неизменная история из БД
        fc_trail — накопленные FC-вызовы текущей итерации (только растёт)
    """
    return context + history + fc_trail


# ── Роутер провайдеров ────────────────────────────────────────────────────────

async def _call_provider(
    session_key: str,
    messages: list[dict],
    system_prompt: str,
    functions: list[dict],
) -> dict:
    """
    Роутер: DeepSeek → GigaChat (fallback) → fallback-ответ.

    Деперсонализация:
      DeepSeek  — все user-сообщения деперсонализируются перед отправкой
      GigaChat  — данные передаются как есть (российский сервис, 152-ФЗ)

    При ошибке DeepSeek → переключает провайдер и повторяет через GigaChat.
    При ошибке обоих   → уведомляет директора, возвращает fallback-ответ.
    """
    from app.ai import clients as ai_clients
    from app.leads.notifier import notify_director_error

    deepseek_client = ai_clients.deepseek_client
    gigachat_client = ai_clients.gigachat_client

    if deepseek_client is None or gigachat_client is None:
        logger.error(
            "[AGENT] AI-клиенты не инициализированы — "
            "init_clients() не был вызван в lifespan. session=%s", session_key
        )
        return {
            "finish_reason": "stop",
            "message": {"content": _fallback_reply()},
        }

    provider = await get_provider(session_key)

    try:
        if provider == "deepseek":
            safe_messages = await depersonalize_history(session_key, messages)
            try:
                return await deepseek_client.chat(
                    messages=safe_messages,
                    system_prompt=system_prompt,
                    functions=functions,
                    session_key=session_key,
                )
            except Exception as e:
                logger.warning(
                    "[FALLBACK] DeepSeek→GigaChat | session=%s | %s",
                    session_key, type(e).__name__,
                )
                await set_provider(session_key, "gigachat")
                return await gigachat_client.chat(
                    messages=messages,
                    system_prompt=system_prompt,
                    functions=functions,
                    session_key=session_key,
                )
        else:
            return await gigachat_client.chat(
                messages=messages,
                system_prompt=system_prompt,
                functions=functions,
                session_key=session_key,
            )

    except Exception as e:
        logger.error("[BOTH DOWN] session=%s | %s", session_key, type(e).__name__)
        try:
            await notify_director_error(session_key, type(e).__name__)
        except Exception:
            pass
        return {
            "finish_reason": "stop",
            "message": {"content": _fallback_reply()},
        }


# ── Агентный цикл ─────────────────────────────────────────────────────────────

async def run_agent(
    messages: list,
    lead: Lead,
    progress: ChecklistProgress,
    session_key: str = "default",
) -> tuple[str, AgentResult]:
    """
    Публичный интерфейс. Обёртка с общим таймаутом над _run_agent_inner.
    Гарантирует ответ клиенту даже при зависании провайдера.
    """
    try:
        return await asyncio.wait_for(
            _run_agent_inner(messages, lead, progress, session_key),
            timeout=_AGENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(
            "Agent: общий таймаут %.0fs превышен | session=%s",
            _AGENT_TIMEOUT, session_key,
        )
        ctx = AgentContext(lead=lead, progress=progress)
        return _fallback_reply(), ctx.to_result()


async def _run_agent_inner(
    messages: list,
    lead: Lead,
    progress: ChecklistProgress,
    session_key: str,
) -> tuple[str, AgentResult]:
    """
    Внутренний агентный цикл без таймаута — вызывается через run_agent.
    """
    ctx = AgentContext(lead=lead, progress=progress)

    # Статический промпт — кэшируется у DeepSeek (prefix caching)
    system_prompt = build_system_prompt()

    # История из БД — неизменна в рамках одного запроса
    history_messages: list[dict] = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]

    # FC-trail — накапливает записи о вызванных функциях, не сбрасывается
    fc_trail: list[dict] = []

    # Начальная сборка
    context_messages = _build_context_message(ctx.lead, ctx.progress)
    working_messages = _rebuild_messages(context_messages, history_messages, fc_trail)

    for iteration in range(_MAX_ITERATIONS):
        logger.debug("Agent iteration %d/%d", iteration + 1, _MAX_ITERATIONS)

        response = await _call_provider(
            session_key=session_key,
            messages=working_messages,
            system_prompt=system_prompt,
            functions=FUNCTIONS,
        )

        finish_reason = response.get("finish_reason", "stop")
        message       = response.get("message", {})
        content       = (message.get("content") or "").strip()

        # ── Option A: GigaChat-2-Max structured FC ────────────────────────────
        if finish_reason == "function_call" and "function_call" in message:
            fc        = message["function_call"]
            func_name = fc.get("name", "")
            raw_args  = fc.get("arguments", {})
            func_args = (
                json.dumps(raw_args, ensure_ascii=False)
                if isinstance(raw_args, dict)
                else (raw_args or "{}")
            )
            logger.info("[FC-A] %s", func_name)
            dispatch(func_name, func_args, ctx)
            # Обновляем контекст, добавляем в fc_trail — история не сбрасывается
            context_messages = _build_context_message(ctx.lead, ctx.progress)
            fc_trail.append({"role": "assistant", "content": f"[called {func_name}]"})
            working_messages = _rebuild_messages(context_messages, history_messages, fc_trail)
            continue

        # ── Option D: DeepSeek tool_calls (массив) ────────────────────────────
        if finish_reason == "tool_calls":
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                logger.warning("Agent: tool_calls пустой список | session=%s", session_key)
                if content:
                    return await _maybe_personalize(session_key, content), ctx.to_result()
                return _fallback_reply(), ctx.to_result()

            called: list[str] = []
            tool_messages: list[dict] = []

            for tool_call in tool_calls:
                fn       = tool_call["function"]["name"]
                fn_args  = tool_call["function"].get("arguments") or "{}"

                if fn not in KNOWN_FUNCTIONS:
                    logger.warning("[FC-D] Неизвестная функция: %s — пропускаем", fn)
                    continue

                logger.info("[FC-D] %s", fn)
                result = dispatch(fn, fn_args, ctx)
                called.append(fn)
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": result,
                })

            if not called:
                logger.warning("Agent: все tool_calls отклонены | session=%s", session_key)
                continue

            # Добавляем assistant-сообщение с оригинальным tool_calls
            fc_trail.append({
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            })
            # Добавляем все tool-сообщения
            fc_trail.extend(tool_messages)

            context_messages = _build_context_message(ctx.lead, ctx.progress)
            working_messages = _rebuild_messages(context_messages, history_messages, fc_trail)
            continue

        # ── Option B: текстовый FC старого GigaChat ───────────────────────────
        func_name, func_args, remaining_text = _parse_function_call(content)
        if func_name:
            logger.info("[FC-B] %s", func_name)
            dispatch(func_name, func_args or "{}", ctx)
            context_messages = _build_context_message(ctx.lead, ctx.progress)
            fc_trail.append({"role": "assistant", "content": f"[called {func_name}]"})
            working_messages = _rebuild_messages(context_messages, history_messages, fc_trail)

            if remaining_text:
                reply = await _maybe_personalize(session_key, remaining_text)
                logger.info("Agent done in %d iter (FC-B+text)", iteration + 1)
                return reply, ctx.to_result()
            continue

        # ── Option C: текстовый ответ ─────────────────────────────────────────
        if content:
            reply = await _maybe_personalize(session_key, content)
            logger.info("Agent done in %d iter (text)", iteration + 1)
            return reply, ctx.to_result()

        # Пустой ответ — нештатная ситуация
        logger.warning("Agent: пустой ответ на итерации %d", iteration + 1)
        return _fallback_reply(), ctx.to_result()

    logger.warning("Agent: превышен лимит итераций (%d) | session=%s",
                   _MAX_ITERATIONS, session_key)
    return _fallback_reply(), ctx.to_result()