"""Telegram-бот: события файлом, текстом и голосом; вопросы текстом и голосом; карточка и справка."""

from __future__ import annotations

import logging
import threading

import httpx

from . import analytics, config, db, ingest, llm, qa
from .db import Event, Org, session

log = logging.getLogger("bot")
API = "https://api.telegram.org"

HELP = (
    "Я — CRM студии «Северный мост». Что умею:\n\n"
    "📎 Пришлите файл события (расшифровка, письмо, записка, новость, экспорт Telegram, голосовая mp3, таблица xlsx) — "
    "разложу по реестрам и пришлю ссылку на карточку.\n"
    "✍️ Длинный текст — тоже событие. 🎙 Голосовое сообщение — событие или вопрос, пойму сам.\n"
    "❓ Вопрос текстом или голосом — отвечу со ссылками на события.\n\n"
    "Команды:\n"
    "/orgs — все организации\n"
    "/org Терра — карточка клиента\n"
    "/brief Терра — справка к встрече\n"
    "/stale — протухшие сделки и риски\n"
    "/funnel — воронка и менеджеры\n"
    f"\nСайт: {config.PUBLIC_URL}"
)


def tg(method: str, **params):
    if not config.TG_TOKEN:
        return None
    r = httpx.post(f"{API}/bot{config.TG_TOKEN}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        log.warning("telegram %s: %s", method, data)
    return data


def send(chat_id, text: str) -> None:
    text = text or "…"
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=chat_id, text=text[i : i + 3900], disable_web_page_preview=True)


def download(file_id: str) -> tuple[bytes, str]:
    info = tg("getFile", file_id=file_id) or {}
    path = (info.get("result") or {}).get("file_path", "")
    r = httpx.get(f"{API}/file/bot{config.TG_TOKEN}/{path}", timeout=120)
    r.raise_for_status()
    return r.content, path.rsplit("/", 1)[-1]


def set_webhook() -> str:
    if not config.TG_TOKEN:
        return "токен бота не задан"
    if config.PUBLIC_URL.startswith("http://localhost"):
        return "нет публичного адреса"
    url = f"{config.PUBLIC_URL}/tg"
    res = tg("setWebhook", url=url, secret_token=config.TG_SECRET, allowed_updates=["message"]) or {}
    tg(
        "setMyCommands",
        commands=[
            {"command": "orgs", "description": "все организации"},
            {"command": "org", "description": "карточка клиента: /org Терра"},
            {"command": "brief", "description": "справка к встрече: /brief Терра"},
            {"command": "stale", "description": "протухшие сделки и риски"},
            {"command": "funnel", "description": "воронка и менеджеры"},
            {"command": "help", "description": "что умеет бот"},
        ],
    )
    return "ок" if res.get("ok") else f"ошибка: {res}"


def _notify(ev: Event, text: str) -> None:
    send(ev.chat_id, text)


ingest.notify = _notify


def with_sources(text: str) -> str:
    codes = qa.codes_in(text)
    if not codes:
        return text
    links = "\n".join(f"{c}: {config.PUBLIC_URL}/e/{c.replace('#', '%23')}" for c in codes[:8])
    return f"{text}\n\nИсточники:\n{links}"


def handle_update(update: dict) -> None:
    threading.Thread(target=_handle, args=(update,), daemon=True).start()


def _handle(update: dict) -> None:
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if not chat:
        return
    try:
        _route(msg, chat)
    except Exception as e:  # noqa: BLE001
        log.exception("bot error")
        send(chat, f"⚠️ Ошибка: {str(e)[:400]}")


def _accepted(chat, ev: Event, dup: bool) -> None:
    if dup:
        send(chat, f"ℹ️ Событие {ev.code} уже загружено ({ev.status}). Повтор не добавляю: {config.PUBLIC_URL}/e/{ev.code}")
    else:
        send(chat, f"📥 Принял {ev.code} ({ev.channel}). Разбираю — пришлю результат, обычно 20–60 секунд.")


def _route(msg: dict, chat) -> None:
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if msg.get("document"):
        doc = msg["document"]
        data, path = download(doc["file_id"])
        ev, dup = ingest.create_event(doc.get("file_name") or path, data, chat_id=chat)
        return _accepted(chat, ev, dup)

    if msg.get("audio"):
        a = msg["audio"]
        data, path = download(a["file_id"])
        ev, dup = ingest.create_event(a.get("file_name") or path or "audio.mp3", data, chat_id=chat)
        return _accepted(chat, ev, dup)

    if msg.get("voice"):
        data, path = download(msg["voice"]["file_id"])
        send(chat, "🎙 Распознаю голосовое…")
        said = llm.transcribe(data, path or "voice.ogg")
        if not said:
            return send(chat, "Не удалось распознать речь.")
        if qa.is_question(said):
            with session() as s:
                ans = qa.answer(s, said)
            return send(chat, f"Вопрос: «{said}»\n\n" + with_sources(ans))
        ev, dup = ingest.create_event(text=said, chat_id=chat, channel_hint="голосовая")
        with session() as s:
            e = s.get(Event, ev.id)
            e.is_audio = True
            s.commit()
        return _accepted(chat, ev, dup)

    if not text:
        return send(chat, HELP)

    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        return _command(chat, cmd, arg)

    if qa.is_question(text):
        with session() as s:
            ans = qa.answer(s, text)
        return send(chat, with_sources(ans))

    ev, dup = ingest.create_event(text=text, chat_id=chat)
    _accepted(chat, ev, dup)


def _command(chat, cmd: str, arg: str) -> None:
    if cmd in ("/start", "/help"):
        return send(chat, HELP)
    with session() as s:
        if cmd == "/orgs":
            from sqlalchemy import select

            orgs = list(s.scalars(select(Org).order_by(Org.name)))
            if not orgs:
                return send(chat, "Пока пусто. Пришлите первое событие.")
            return send(chat, "\n".join(f"• {o.name} — {config.PUBLIC_URL}/org/{o.id}" for o in orgs))
        if cmd in ("/org", "/brief"):
            if not arg:
                return send(chat, f"Напишите название: {cmd} Терра")
            org = qa.find_org_by_text(s, arg)
            if org is None:
                return send(chat, f"Не нашёл «{arg}». Список: /orgs")
            if cmd == "/brief":
                send(chat, f"Готовлю справку по «{org.name}»…")
                return send(chat, with_sources(qa.brief(s, org)) + f"\n\nКарточка: {config.PUBLIC_URL}/org/{org.id}")
            w = db.latest_wiki(s, org.id)
            head = f"{org.name}\n{config.PUBLIC_URL}/org/{org.id}\n\n"
            if w:
                return send(chat, head + f"Что изменилось: {w.changes}\n\n" + w.text)
            return send(chat, head + "Вики ещё не собрана.")
        if cmd == "/stale":
            deals = [d for d in analytics.all_deals(s) if d.stale or d.risks]
            if not deals:
                return send(chat, "Протухших сделок и рисков нет.")
            out = [f"На {analytics.as_of(s)} (протухла = открытая сделка без живого контакта больше 42 дней):"]
            for d in deals:
                p = d.project
                line = f"• {p.org.name} — {p.product or p.title}, «{p.stage}», {p.manager}: "
                line += f"последний контакт {d.last_contact or 'нет'} [{d.last_contact_event}]"
                if d.stale:
                    line += f", {d.days} дн. — протухла"
                if d.risks:
                    line += "; риск: " + "; ".join(f"{r.text} [{r.event_code}]" for r in d.risks)
                out.append(line)
            return send(chat, "\n".join(out))
        if cmd == "/funnel":
            out = ["Воронка по старой таблице (дубли не считаются):"]
            for m in [None, *analytics.MANAGERS]:
                fn = analytics.funnel(s, m)
                out.append(
                    f"{'Всего' if m is None else m}: "
                    + " → ".join(f"{r['stage']} {r['count']}" + (f" ({r['conv']}%)" if r.get("conv") is not None else "") for r in fn)
                )
            for r in analytics.managers(s):
                out.append(f"{r['manager']}: сделок {r['deals']}, пропущенных дат {r['missing']}, протухших {r['stale']}")
            out.append(analytics.RULE_WORSE)
            out.append(f"{config.PUBLIC_URL}/funnel")
            return send(chat, "\n".join(out))
        if cmd == "/reset":
            if not config.RESET_CODE or arg != config.RESET_CODE:
                return send(chat, "Сброс выключен или код неверный.")
            db.reset_db()
            return send(chat, "База очищена.")
    send(chat, "Не знаю такую команду. /help")
