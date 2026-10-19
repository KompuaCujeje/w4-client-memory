"""Обращение к ИИ через ProxyAPI: текст, JSON и распознавание речи."""

from __future__ import annotations

import io
import json
import logging
import re

from openai import OpenAI

from . import config

log = logging.getLogger("llm")
_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not config.LLM_API_KEY:
            raise RuntimeError("Не задан ключ к ИИ: переменная LLM_API_KEY в настройках сервиса")
        _client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL, timeout=180, max_retries=2)
    return _client


def chat_text(system: str, user: str, model: str | None = None, temperature: float = 0.1) -> str:
    r = client().chat.completions.create(
        model=model or config.LLM_MODEL,
        temperature=temperature,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (r.choices[0].message.content or "").strip()


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        raw = m.group(0)
    return json.loads(raw)


def chat_json(system: str, user: str, model: str | None = None) -> dict:
    kwargs = dict(
        model=model or config.LLM_MODEL,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    try:
        r = client().chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception as e:  # некоторые модели не знают response_format — повторим без него
        log.warning("json_object не принят (%s), пробую без него", e)
        r = client().chat.completions.create(**kwargs)
    return _parse_json(r.choices[0].message.content or "{}")


def transcribe(data: bytes, filename: str) -> str:
    f = io.BytesIO(data)
    f.name = filename or "audio.ogg"
    r = client().audio.transcriptions.create(model=config.STT_MODEL, file=f, language="ru")
    return (getattr(r, "text", "") or "").strip()


def health() -> str:
    try:
        out = chat_text("Отвечай одним словом.", "Скажи: работает", model=config.LLM_MODEL_FAST)
        return f"ок ({config.LLM_MODEL_FAST}: {out[:40]})"
    except Exception as e:
        return f"ошибка: {e}"
