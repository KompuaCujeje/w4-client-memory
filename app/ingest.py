"""Приём событий: файл → событие → разбор ИИ → реестры, факты, вики. События разбираются строго по одному."""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import queue
import re
import threading
import time
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import llm, prompts
from .db import (
    CHANNEL_DEPTH,
    CLOSED,
    MANAGERS,
    PRODUCTS,
    STAGES,
    Conflict,
    Event,
    Fact,
    Job,
    Link,
    Org,
    Person,
    Project,
    Wiki,
    latest_wiki,
    session,
)
from .names import org_key, same_person

log = logging.getLogger("ingest")

AUDIO_EXT = (".mp3", ".ogg", ".oga", ".m4a", ".wav", ".opus", ".webm", ".mp4")
TABLE_EXT = (".xlsx", ".xlsm")

# Колбэк «событие разобрано» — бот ставит сюда отправку сообщения
notify: Callable[[Event, str], None] | None = None


# ---------- метаданные события ----------


def norm_channel(raw: str) -> str:
    r = (raw or "").lower()
    if "расшифров" in r or "встреч" in r or "transcript" in r:
        return "расшифровка встречи"
    if "telegram" in r or "переписк" in r or "чат" in r or "whatsapp" in r:
        return "переписка"
    if "голос" in r or "voice" in r or "аудио" in r:
        return "голосовая"
    if "письм" in r or "почт" in r or "email" in r or "e-mail" in r:
        return "письмо"
    if "записк" in r or "заметк" in r or "note" in r:
        return "записка"
    if "новост" in r or "news" in r or "стать" in r:
        return "новость"
    if "таблиц" in r or "выгрузк" in r:
        return "таблица"
    return "другое"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return meta, text[m.end() :]


def parse_filename(name: str) -> dict:
    out: dict = {}
    base = re.sub(r"\.[^.]+$", "", name or "")
    m = re.match(r"^([A-Za-zА-Яа-я]\d+)[_\- ]", base)
    if m:
        out["id"] = m.group(1).upper().replace("Е", "E")
    d = re.search(r"(\d{4}-\d{2}-\d{2})", base)
    if d:
        out["date"] = d.group(1)
    tail = re.sub(r"^.*\d{4}-\d{2}-\d{2}[_\- ]?", "", base) if d else ""
    if tail:
        out["channel"] = tail
    return out


def parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


def next_code(s: Session, prefix: str = "U") -> str:
    n = s.scalar(select(func.count(Event.id)).where(Event.code.like(f"{prefix}%"))) or 0
    while True:
        n += 1
        code = f"{prefix}{n:03d}"
        if not s.scalar(select(Event.id).where(Event.code == code)):
            return code


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


# ---------- создание события ----------


def create_event(
    filename: str = "",
    data: bytes | None = None,
    text: str = "",
    chat_id: str = "",
    channel_hint: str = "",
    date_hint: str = "",
) -> tuple[Event, bool]:
    """Сохраняет событие и ставит в очередь. Возвращает (событие, это_повтор)."""
    fname = filename or ""
    low = fname.lower()
    meta = parse_filename(fname)
    is_audio = low.endswith(AUDIO_EXT)
    is_table = low.endswith(TABLE_EXT)
    body = ""
    if data is not None and not is_audio and not is_table:
        body = decode(data)
    elif text:
        body = text
    fm, rest = parse_frontmatter(body) if body else ({}, "")
    meta.update({k: v for k, v in fm.items() if v})

    with session() as s:
        code = (meta.get("id") or "").upper()
        if code:
            old = s.scalar(select(Event).where(Event.code == code))
            if old is not None:
                return old, True
        else:
            code = next_code(s)
        channel = norm_channel(channel_hint or meta.get("channel", ""))
        if is_audio:
            channel = "голосовая"
        if is_table:
            channel = "таблица"
        if channel == "другое" and fm.get("channel"):
            channel = norm_channel(fm["channel"])
        ev = Event(
            code=code,
            date=parse_date(date_hint) or parse_date(meta.get("date")),
            channel=channel,
            author=meta.get("author", ""),
            source=meta.get("source", ""),
            filename=fname,
            text=(rest if fm else body).strip(),
            is_audio=is_audio,
            chat_id=str(chat_id or ""),
        )
        s.add(ev)
        s.commit()
        if is_audio or is_table:
            _blobs[ev.id] = data or b""
        enqueue(ev.id)
        return ev, False


# Аудио и таблицы держим в памяти до разбора (на бесплатном Render диск не постоянный)
_blobs: dict[int, bytes] = {}

# ---------- очередь: события строго по одному ----------

_q: queue.Queue[int] = queue.Queue()
_worker: threading.Thread | None = None
busy = threading.Event()


def enqueue(event_id: int) -> None:
    _q.put(event_id)


def start_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    with session() as s:  # недоразобранное после перезапуска — снова в очередь (текст уже в базе)
        for ev in s.scalars(select(Event).where(Event.status == "pending").order_by(Event.id)):
            if ev.is_audio and not ev.text:
                ev.status, ev.error = "error", "сервис перезапустился до распознавания — пришлите голосовую ещё раз"
                continue
            _q.put(ev.id)
        s.commit()
    _worker = threading.Thread(target=_loop, daemon=True)
    _worker.start()


def _loop() -> None:
    while True:
        eid = _q.get()
        busy.set()
        try:
            process_event(eid)
        except Exception as e:  # noqa: BLE001
            log.exception("event %s failed", eid)
            with session() as s:
                ev = s.get(Event, eid)
                if ev:
                    ev.status, ev.error = "error", str(e)[:2000]
                    s.commit()
                    _notify(ev, f"⚠️ {ev.code}: не удалось разобрать — {str(e)[:300]}")
        finally:
            busy.clear()


def _notify(ev: Event, text: str) -> None:
    if notify and ev.chat_id:
        try:
            notify(ev, text)
        except Exception:  # noqa: BLE001
            log.exception("notify failed")


def wait_idle(timeout: float = 600) -> None:
    """Для тестов: дождаться, пока очередь опустеет."""
    t0 = time.time()
    while (not _q.empty() or busy.is_set()) and time.time() - t0 < timeout:
        time.sleep(0.05)


# ---------- разбор ----------


def process_event(event_id: int) -> None:
    t0 = time.time()
    with session() as s:
        ev = s.get(Event, event_id)
        if ev is None or ev.status == "done":
            return
        if ev.channel == "таблица":
            from .table import import_table

            import_table(s, ev, _blobs.pop(ev.id, b""))
        else:
            if ev.is_audio and not ev.text:
                ev.text = llm.transcribe(_blobs.pop(ev.id, b""), ev.filename or "voice.ogg")
                s.commit()
            if ev.channel == "переписка":
                split_chat(s, ev)
            touched = extract_and_apply(s, ev)
            for oid in touched:
                rewrite_wiki(s, oid, ev)
        ev.status = "done"
        ev.processed_at = dt.datetime.now(dt.timezone.utc)
        ev.seconds = round(time.time() - t0, 1)
        s.commit()
        _notify(ev, done_message(s, ev))


CHAT_LINE = re.compile(r"^\[(\d{2}\.\d{2}\.\d{4})(?:,?\s+(\d{1,2}:\d{2}))?\]\s*([^:]+):\s*(.*)$")


def split_chat(s: Session, ev: Event) -> None:
    """Экспорт Telegram → отдельные сообщения-события с датой и автором (код E07.1, E07.2…)."""
    if s.scalar(select(Event.id).where(Event.parent == ev.code)):
        return
    n = 0
    for line in ev.text.splitlines():
        m = CHAT_LINE.match(line.strip())
        if not m:
            continue
        n += 1
        d = parse_date(m.group(1))
        s.add(
            Event(
                code=f"{ev.code}.{n}",
                parent=ev.code,
                date=d,
                channel="переписка",
                author=m.group(3).strip(),
                title=m.group(2) or "",
                text=m.group(4).strip(),
                status="done",
            )
        )
        if ev.date is None and d:
            ev.date = d
    s.commit()


def registry_snapshot(s: Session) -> str:
    orgs = [{"id": o.id, "name": o.name, "aliases": o.aliases, "city": o.city} for o in s.scalars(select(Org))]
    people = []
    for p in s.scalars(select(Person)):
        people.append(
            {
                "id": p.id,
                "name": p.name,
                "aliases": p.aliases,
                "jobs": [{"org_id": j.org_id, "role": j.role, "current": j.current} for j in p.jobs],
                "note": p.note,
            }
        )
    projects = [
        {
            "id": j.id,
            "org_id": j.org_id,
            "title": j.title,
            "product": j.product,
            "stage": j.stage,
            "paused": j.paused,
            "manager": j.manager,
        }
        for j in s.scalars(select(Project).where(Project.duplicate_of.is_(None)))
    ]
    facts = [
        {
            "fact_id": f.id,
            "org_id": f.org_id,
            "kind": f.kind,
            "text": f.text,
            "value": f.value,
            "event": f.event_code,
            "channel": f.channel,
            "done": f.done,
        }
        for f in s.scalars(
            select(Fact).where(Fact.kind.in_(["budget", "deadline", "scope", "decision", "promise", "request", "waiting", "risk"]))
        )
        if not f.superseded_by
    ]
    return json.dumps({"orgs": orgs, "people": people, "projects": projects, "facts": facts}, ensure_ascii=False)


def event_payload(s: Session, ev: Event) -> str:
    head = f"Код: {ev.code}\nДата: {ev.date or 'нет'}\nКанал: {ev.channel}\nАвтор: {ev.author or '—'}\n"
    if ev.source:
        head += f"Источник: {ev.source}\n"
    if ev.channel == "переписка":
        msgs = list(s.scalars(select(Event).where(Event.parent == ev.code).order_by(Event.id)))
        if msgs:
            body = "\n".join(f"[{m.code}] {m.date} {m.title} {m.author}: {m.text}" for m in msgs)
            return head + "Сообщения (в facts.msg указывай код сообщения):\n" + body
    if ev.is_audio:
        head += "Это автоматическое распознавание голосовой заметки менеджера — имена и числа могут быть искажены.\n"
    return head + "Текст:\n" + ev.text


def extract_and_apply(s: Session, ev: Event) -> list[int]:
    user = (
        "РЕЕСТР СЕЙЧАС (JSON):\n"
        + registry_snapshot(s)
        + "\n\nСОБЫТИЕ:\n"
        + event_payload(s, ev)
        + "\n\n"
        + prompts.EXTRACT_FORMAT
    )
    data = llm.chat_json(prompts.EXTRACT_SYSTEM, user)
    return apply_extraction(s, ev, data)


def _seen(names, text: str) -> list[str]:
    """Синоним берём, только если он есть в тексте события: ИИ не должен придумывать отчества и написания."""
    low = (text or "").lower().replace("ё", "е")
    out = []
    for n in names or []:
        n = (n or "").strip()
        if len(n) > 2 and n[0].isupper() and n.lower().replace("ё", "е") in low:  # «собственник» — роль, не имя
            out.append(n)
    return out


def _add_aliases(obj, names) -> None:
    al = list(obj.aliases or [])
    for n in names or []:
        n = (n or "").strip()
        if n and n != obj.name and n not in al:
            al.append(n)
    obj.aliases = al


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None


def find_org(s: Session, name: str, oid=None) -> Org | None:
    """Сначала точное совпадение имени или синонима. id от ИИ — только если имя с ним не спорит."""
    key = org_key(name)
    exact = _known_org(s, key)
    if exact is not None:
        return exact
    o = s.get(Org, _int(oid)) if _int(oid) else None
    return o  # ИИ узнал организацию под новым именем (А6) — имя станет синонимом


def _known_org(s: Session, key: str) -> Org | None:
    if not key:
        return None
    for o in s.scalars(select(Org)):
        if org_key(o.name) == key or any(org_key(a) == key for a in o.aliases or []):
            return o
    return None


def find_person(s: Session, name: str, pid=None) -> Person | None:
    for p in s.scalars(select(Person)):
        if same_person(p.name, name) or any(same_person(a, name) for a in p.aliases or []):
            return p
    p = s.get(Person, _int(pid)) if _int(pid) else None
    return p  # «Анна Р.» = «Анна Игоревна Римская» — узнаёт ИИ


def _merge_names(cur: str, new: str) -> str:
    """Дополняет имя недостающими словами из нового написания того же человека."""
    a, b = cur.split(), new.split()
    if not a or not b or a[0].lower().rstrip(".") != b[0].lower().rstrip(".") or len(b[0].rstrip(".")) < 2:
        return cur if len(b) <= len(a) or not set(w.lower() for w in a) <= set(w.lower() for w in b) else new
    extra = [w for w in b[1:] if w.lower() not in {x.lower() for x in a} and len(w.rstrip(".")) > 1]
    return " ".join(a + extra) if extra and len(a) + len(extra) <= 3 else cur


def _is_ours(name: str) -> bool:
    n = (name or "").lower()
    return any(x in n for x in ("ларин", "соболь", "северный мост"))


def apply_extraction(s: Session, ev: Event, d: dict) -> list[int]:
    source_text = (ev.text or "") + " " + " ".join(
        m.text + " " + m.author for m in s.scalars(select(Event).where(Event.parent == ev.code))
    ) + " " + (ev.author or "")
    date = ev.date or parse_date(d.get("date")) or dt.date.today()
    if ev.date is None:
        ev.date = date
    ev.summary = (d.get("summary") or "")[:2000]
    ev.internal = bool(d.get("internal"))
    ev.same_meeting_as = (d.get("same_meeting_as") or "") or ""
    ev.live_contact = bool(d.get("live_contact")) and not ev.internal and not ev.same_meeting_as
    if ev.channel in ("новость", "таблица"):
        ev.live_contact = False
    ev.contact_kind = d.get("contact_kind") or ""

    refs: dict[str, tuple[str, object]] = {}
    touched: list[int] = []

    def touch(oid: int | None) -> None:
        if oid and oid not in touched:
            touched.append(oid)

    for o in d.get("orgs") or []:
        name = (o.get("name") or "").strip()
        if not name or _is_ours(name):
            continue
        org = find_org(s, name, o.get("id"))
        if org is None:
            org = Org(name=name, aliases=[], city=o.get("city") or "", about=o.get("about") or "", first_event=ev.code)
            s.add(org)
            s.flush()
        else:
            if not org.city and o.get("city"):
                org.city = o["city"]
            if not org.about and o.get("about"):
                org.about = o["about"]
        _add_aliases(org, [name] + _seen(o.get("aliases"), source_text))
        refs[o.get("ref") or f"o{org.id}"] = ("org", org)
        touch(org.id)

    low_text = source_text.lower().replace("ё", "е")
    for p in d.get("people") or []:
        name = (p.get("name") or "").strip()
        # слова имени, которых нет в событии (додуманное отчество), отбрасываем
        kept = [w for w in name.split() if w.lower().replace("ё", "е").rstrip(".") in low_text]
        name = " ".join(kept) if kept else name
        if not name or _is_ours(name):
            continue
        person = find_person(s, name, p.get("id"))
        if person is None:
            person = Person(name=name, aliases=[], first_event=ev.code, note=p.get("note") or "")
            s.add(person)
            s.flush()
        else:
            merged = _merge_names(person.name, name)
            if merged != person.name:
                old_name, person.name = person.name, merged
                _add_aliases(person, [old_name])  # «Елена Павловна» + «Елена Власова» → «Елена Павловна Власова»
        _add_aliases(person, [name] + _seen(p.get("aliases"), source_text))
        refs[p.get("ref") or f"p{person.id}"] = ("person", person)
        org_ref = refs.get(p.get("org_ref") or "")
        if org_ref and org_ref[0] == "org":
            org = org_ref[1]
            job = next((j for j in person.jobs if j.org_id == org.id), None)
            if job is None:
                job = Job(person_id=person.id, org_id=org.id, role=p.get("role") or "", since=date, event_code=ev.code)
                s.add(job)
                s.flush()
                s.refresh(person)
            elif p.get("role") and not job.role:
                job.role = p["role"]
            if p.get("left_org"):
                job.current, job.until = False, date
            if (person.note or "").startswith("организация не названа"):
                person.note = ""
            touch(org.id)
        elif not person.jobs and not person.note:
            person.note = "организация не названа"

    for j in d.get("projects") or []:
        org_ref = refs.get(j.get("org_ref") or "")
        if not org_ref or org_ref[0] != "org":
            continue
        org = org_ref[1]
        product = j.get("product") if j.get("product") in PRODUCTS else ""
        proj = None
        if j.get("id"):
            proj = s.get(Project, int(j["id"]))
        if proj is None and product:
            proj = s.scalar(
                select(Project).where(
                    Project.org_id == org.id,
                    Project.product == product,
                    Project.stage != CLOSED,
                    Project.duplicate_of.is_(None),
                )
            )
        stage = j.get("stage") if j.get("stage") in STAGES + [CLOSED] else None
        if proj is None:
            proj = Project(
                org_id=org.id,
                title=j.get("title") or product or "проект",
                product=product,
                manager=j.get("manager") if j.get("manager") in MANAGERS else _manager_from_author(ev.author),
                stage=stage or "знакомство",
                stage_dates={},
                stage_history=[],
                created=date,
                source=ev.code,
            )
            s.add(proj)
            s.flush()
            _set_stage(proj, "знакомство", date, ev.code)
        if stage:
            _set_stage(proj, stage, date, ev.code)
        if j.get("paused") is not None:
            proj.paused = bool(j["paused"])
        if j.get("close_reason"):
            proj.close_reason = j["close_reason"]
        if not proj.manager:
            proj.manager = _manager_from_author(ev.author)
        refs[j.get("ref") or f"j{proj.id}"] = ("project", proj)
        touch(org.id)

    for f in d.get("facts") or []:
        target = refs.get(f.get("about") or "")
        org_id = person_id = project_id = None
        if target:
            kind, obj = target
            if kind == "org":
                org_id = obj.id
            elif kind == "person":
                person_id = obj.id
                cur = next((jb for jb in obj.jobs if jb.current), None)
                org_id = cur.org_id if cur else None
            else:
                project_id, org_id = obj.id, obj.org_id
        msg = (f.get("msg") or "").strip()
        code = msg if msg.startswith(ev.code + ".") else ev.code
        fact = Fact(
            org_id=org_id,
            person_id=person_id,
            project_id=project_id,
            kind=f.get("kind") or "other",
            text=(f.get("text") or "")[:2000],
            value=(f.get("value") or "")[:300],
            quote=(f.get("quote") or "")[:1000],
            event_code=code,
            date=date,
            channel=ev.channel,
            confirmed=CHANNEL_DEPTH.get(ev.channel, 0) >= 3 or ev.channel == "новость",
            confidential=bool(f.get("confidential")),
        )
        s.add(fact)
        s.flush()
        _supersede(s, fact, ev)
        touch(org_id)

    for dn in d.get("done") or []:
        try:
            fact = s.get(Fact, int(dn.get("fact_id")))
        except (TypeError, ValueError):
            fact = None
        if fact:
            fact.done, fact.done_event = True, ev.code
            touch(fact.org_id)

    for c in d.get("conflicts") or []:
        target = refs.get(c.get("about") or "")
        oid = None
        if target:
            oid = target[1].id if target[0] == "org" else getattr(target[1], "org_id", None)
        s.add(
            Conflict(
                org_id=oid,
                subject=c.get("subject") or "",
                version_a=c.get("other_version") or "",
                event_a=c.get("other_event") or "",
                version_b=c.get("this_version") or "",
                event_b=ev.code,
                verdict=c.get("verdict") or "не решено",
                date=date,
            )
        )
        touch(oid)

    for lk in d.get("links") or []:
        a, b = refs.get(lk.get("a") or ""), refs.get(lk.get("b") or "")
        a_type, a_id = (a[0], str(a[1].id)) if a else ("text", lk.get("a") or "")
        b_type, b_id = (b[0], str(b[1].id)) if b else ("text", lk.get("b") or "")
        if a_id and b_id and lk.get("kind"):
            s.add(Link(a_type=a_type, a_id=a_id, kind=lk["kind"], b_type=b_type, b_id=b_id, event_code=ev.code, quote=lk.get("quote") or ""))

    for t in d.get("topics") or []:
        o = refs.get(t.get("org_ref") or "")
        topic = (t.get("topic") or "").strip().lower()
        if o and o[0] == "org" and topic:
            s.add(Link(a_type="org", a_id=str(o[1].id), kind="интересуется темой", b_type="text", b_id=f"тема: {topic}", event_code=ev.code, quote=t.get("quote") or ""))

    ev.org_ids = touched
    # голосовая/записка в день встречи с той же организацией — та же встреча, не новый контакт
    if ev.channel in ("голосовая", "записка") and not ev.same_meeting_as and touched:
        for other in s.scalars(select(Event).where(Event.date == ev.date, Event.parent == "", Event.id != ev.id)):
            if other.depth > ev.depth and set(other.org_ids or []) & set(touched) and other.live_contact:
                ev.same_meeting_as, ev.live_contact = other.code, False
                break
    s.commit()
    return touched


def _manager_from_author(author: str) -> str:
    a = (author or "").lower()
    if "андрей" in a or "ларин" in a:
        return "Андрей"
    if "ирина" in a or "соболь" in a:
        return "Ирина"
    return ""


def _set_stage(proj: Project, stage: str, date: dt.date, code: str) -> None:
    sd = dict(proj.stage_dates or {})
    hist = list(proj.stage_history or [])
    if stage not in sd:
        sd[stage] = date.isoformat()
    if not hist or hist[-1].get("stage") != stage:
        hist.append({"stage": stage, "date": date.isoformat(), "event": code})
    proj.stage_dates, proj.stage_history, proj.stage = sd, hist, stage


VALUE_KINDS = ("budget", "deadline", "decision")  # объём работ копится пунктами, его смену ИИ пишет новым фактом


def _supersede(s: Session, fact: Fact, ev: Event) -> None:
    """Б2: новое значение суммы/срока/объёма заменяет старое; при той же дате глубокий источник побеждает."""
    if fact.kind not in VALUE_KINDS or not (fact.project_id or fact.org_id):
        return
    q = select(Fact).where(Fact.kind == fact.kind, Fact.id != fact.id, Fact.superseded_by == "")
    q = q.where(Fact.project_id == fact.project_id) if fact.project_id else q.where(Fact.org_id == fact.org_id)
    for old in s.scalars(q):
        if old.event_code.split(".")[0] == fact.event_code.split(".")[0]:
            continue  # несколько пунктов из одного события не заменяют друг друга
        if old.date and fact.date and old.date == fact.date and CHANNEL_DEPTH.get(old.channel, 0) > CHANNEL_DEPTH.get(fact.channel, 0):
            fact.superseded_by = old.event_code  # мельче источник в тот же день — не перебивает
        else:
            old.superseded_by = fact.event_code


# ---------- вики ----------


def org_card_json(s: Session, org_id: int) -> dict:
    o = s.get(Org, org_id)
    if o is None:
        return {}
    people = []
    for j in s.scalars(select(Job).where(Job.org_id == org_id)):
        people.append({"name": j.person.name, "role": j.role, "current": j.current, "since": str(j.since or ""), "until": str(j.until or ""), "event": j.event_code})
    projects = [
        {
            "id": p.id,
            "title": p.title,
            "product": p.product,
            "manager": p.manager,
            "stage": p.stage,
            "history": p.stage_history,
            "paused": p.paused,
            "close_reason": p.close_reason,
            "duplicate_of": p.duplicate_of,
        }
        for p in o.projects
    ]
    facts = [
        {
            "fact_id": f.id,
            "kind": f.kind,
            "text": f.text,
            "value": f.value,
            "event": f.event_code,
            "date": str(f.date or ""),
            "channel": f.channel,
            "confirmed": f.confirmed,
            "confidential": f.confidential,
            "done": f.done,
            "replaced_by": f.superseded_by,
        }
        for f in s.scalars(select(Fact).where(Fact.org_id == org_id).order_by(Fact.date, Fact.id))
    ]
    conflicts = [
        {"subject": c.subject, "a": c.version_a, "a_event": c.event_a, "b": c.version_b, "b_event": c.event_b, "verdict": c.verdict}
        for c in s.scalars(select(Conflict).where(Conflict.org_id == org_id))
    ]
    events = [
        {"code": e.code, "date": str(e.date or ""), "channel": e.channel, "summary": e.summary, "live_contact": e.live_contact}
        for e in s.scalars(select(Event).where(Event.status == "done", Event.parent == "").order_by(Event.date, Event.id))
        if org_id in (e.org_ids or [])
    ]
    return {
        "org": {"id": o.id, "name": o.name, "aliases": o.aliases, "city": o.city, "about": o.about},
        "people": people,
        "projects": projects,
        "facts": facts,
        "conflicts": conflicts,
        "events": events,
    }


def rewrite_wiki(s: Session, org_id: int, ev: Event) -> None:
    card = org_card_json(s, org_id)
    if not card:
        return
    prev = latest_wiki(s, org_id)
    user = (
        "КАРТОЧКА (JSON):\n"
        + json.dumps(card, ensure_ascii=False)
        + "\n\nПРЕЖНЯЯ ВЕРСИЯ ВИКИ:\n"
        + (prev.text if prev else "(нет — это первая версия)")
        + f"\n\nНОВОЕ СОБЫТИЕ: {ev.code} ({ev.channel}, {ev.date}): {ev.summary}\n\n"
        + prompts.WIKI_FORMAT.replace("{code}", ev.code)
    )
    data = llm.chat_json(prompts.WIKI_SYSTEM, user)
    s.add(
        Wiki(
            org_id=org_id,
            version=(prev.version + 1) if prev else 1,
            text=data.get("text") or "",
            changes=data.get("changes") or f"событие [{ev.code}]",
            event_code=ev.code,
        )
    )
    s.commit()


def done_message(s: Session, ev: Event) -> str:
    from . import config

    if ev.channel == "таблица":
        n = s.scalar(select(func.count(Project.id)).where(Project.source.like(f"{ev.code}#%"))) or 0
        d = s.scalar(select(func.count(Project.id)).where(Project.source.like(f"{ev.code}#%"), Project.duplicate_of.is_not(None))) or 0
        return f"✅ {ev.code}: таблица загружена — {n} строк, дублей сделок {d}.\n{config.PUBLIC_URL}/projects"
    names = [o.name for o in (s.get(Org, i) for i in ev.org_ids or []) if o]
    lines = [f"✅ {ev.code} ({ev.channel}, {ev.date}) разобрано за {ev.seconds or 0:.0f} с."]
    if ev.summary:
        lines.append(ev.summary)
    if names:
        lines.append("Организации: " + ", ".join(names))
        for i in ev.org_ids or []:
            lines.append(f"{config.PUBLIC_URL}/org/{i}")
    elif ev.internal:
        lines.append("Внутренняя переписка студии — без новой организации.")
    return "\n".join(lines)


def blob_from_upload(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
