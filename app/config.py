"""Настройки — только из переменных окружения. Секретов в коде нет."""

import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# База: на Render — адрес Postgres, локально — файл SQLite
DATABASE_URL = env("DATABASE_URL", "sqlite:///./crm.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://") :]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://") :]

# ИИ через ProxyAPI (OpenAI-совместимый адрес)
LLM_API_KEY = env("LLM_API_KEY") or env("OPENAI_API_KEY") or env("PROXYAPI_KEY")
LLM_BASE_URL = env("LLM_BASE_URL", "https://api.proxyapi.ru/openai/v1")
LLM_MODEL = env("LLM_MODEL", "gpt-4.1")
LLM_MODEL_FAST = env("LLM_MODEL_FAST", "gpt-4.1-mini")
STT_MODEL = env("STT_MODEL", "gpt-4o-transcribe")  # при ошибке — whisper-1

# Telegram
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
# Секрет вебхука: только буквы и цифры (Telegram передаёт его в заголовке, в адресе его нет)
import hashlib as _h

TG_SECRET = _h.sha256(env("TELEGRAM_WEBHOOK_SECRET", "hook").encode()).hexdigest()[:40]

# Публичный адрес сайта: Render задаёт RENDER_EXTERNAL_URL сам
PUBLIC_URL = (env("PUBLIC_URL") or env("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")

# Код для сброса базы (/reset в боте и на сайте). Пустой — сброс выключен.
RESET_CODE = env("RESET_CODE")

# Тестовый режим без ИИ: ответы берутся из заготовок (только для проверки кода)
FAKE_LLM = env("FAKE_LLM") == "1"
