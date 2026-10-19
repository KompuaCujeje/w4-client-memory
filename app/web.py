"""Сайт: списки с поиском и фильтрами, карточка с вики и лентой событий, воронка, вопросы, загрузка событий."""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import analytics, bot, config, db, ingest, llm, qa
from .db import CLOSED, MANAGERS, STAGES, Conflict, Event, Fact, Job, Org, Person, Project, Wiki, session

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CODE_RE = re.compile(r"\[((?:[EU]\d+(?:[.#]\d+)?)(?:\s*[,;]\s*[EU]\d+(?:[.#]\d+)?)*)\]")

KIND_NAMES = {
    "need": "Задача клиента",
    "budget": "Бюджет",
    "deadline": "Сроки",
    "scope": "Объём работ",
    "decision": "Кто и когда решает",
    "promise": "Мы обещали",
    "request": "Клиент просил",
    "waiting": "Ждём от клиента",
    "news": "Новости",
    "risk": "Риски",
    "personal": "Личное",
    "stage": "Этап",
    "contact": "Контакты",
    "other": "Другое",
}


def code_href(code: str) -> str:
    return "/e/" + code.replace("#", "%23")


def linkify(text: str) -> str:
    """Экранирует текст и превращает [E01] / [E01, E03] в ссылки на события."""
    esc = html.escape(text or "")

    def rep(m):
        codes = re.split(r"\s*[,;]\s*", m.group(1))
        return "[" + ", ".join(f'<a class="src" href="{code_href(c)}">{c}</a>' for c in codes) + "]"

    return CODE_RE.sub(rep, esc)


def md(text: str) -> str:
    """Минимальный markdown: заголовки, списки, жирный, ссылки на события."""
    out, in_list = [], False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        h = re.match(r"^(#{1,4})\s+(.*)$", line)
        li = re.match(r"^\s*[-*•]\s+(.*)$", line)
        body = linkify(h.group(2) if h else li.group(1) if li else line)
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
        if h:
            if in_list:
                out.append("</ul>")
                in_list = False
            lvl = min(len(h.group(1)) + 1, 4)
            out.append(f"<h{lvl}>{body}</h{lvl}>")
        elif li:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{body}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{body}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


templates.env.filters["md"] = md
templates.env.filters["linkify"] = linkify
templates.env.globals.update(code_href=code_href, KIND_NAMES=KIND_NAMES, STAGES=STAGES, CLOSED=CLOSED, MANAGERS=MANAGERS)


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    with session() as s:
        deals = analytics.all_deals(s)
        open_deals = [d for d in deals if d.is_open]
        ctx = dict(
            n_orgs=len(list(s.scalars(select(Org.id)))),
            n_people=len(list(s.scalars(select(Person.id)))),
            n_projects=len([d for d in deals if d.project.duplicate_of is None]),
            n_open=len(open_deals),
            stale=[d for d in open_deals if d.stale],
            risky=[d for d in open_deals if d.risks],
            conflicts=list(s.scalars(select(Conflict).order_by(Conflict.id.desc()))),
            events=list(s.scalars(select(Event).where(Event.parent == "").order_by(Event.id.desc()).limit(12))),
            as_of=analytics.as_of(s),
            funnel=analytics.funnel(s),
            busy=ingest.busy.is_set() or not ingest._q.empty(),
        )
        return render(request, "home.html", **ctx)


@router.get("/orgs", response_class=HTMLResponse)
def orgs(request: Request, q: str = "", stage: str = "", manager: str = ""):
    with session() as s:
        today = analytics.as_of(s)
        rows = []
        for o in s.scalars(select(Org).order_by(Org.name)):
            hay = " ".join([o.name, *o.aliases, o.city]).lower()
            if q and q.lower() not in hay:
                continue
            projs = [p for p in o.projects if p.duplicate_of is None]
            if stage and not any(p.stage == stage for p in projs):
                continue
            if manager and not any(p.manager == manager for p in projs):
                continue
            states = [analytics.deal_state(s, p, today) for p in projs]
            last = max([d.last_contact for d in states if d.last_contact] or [None]) if states else None
            rows.append((o, projs, states, last))
        return render(request, "orgs.html", rows=rows, q=q, stage=stage, manager=manager)


@router.get("/org/{org_id}", response_class=HTMLResponse)
def org_card(request: Request, org_id: int, v: int | None = None):
    with session() as s:
        o = s.get(Org, org_id)
        if o is None:
            return HTMLResponse("Нет такой организации", 404)
        today = analytics.as_of(s)
        versions = list(s.scalars(select(Wiki).where(Wiki.org_id == org_id).order_by(Wiki.version.desc())))
        wiki = next((w for w in versions if w.version == v), versions[0] if versions else None)
        facts = list(s.scalars(select(Fact).where(Fact.org_id == org_id).order_by(Fact.date, Fact.id)))
        grouped: dict[str, list[Fact]] = {}
        for f in facts:
            grouped.setdefault(f.kind, []).append(f)
        order = [k for k in KIND_NAMES if k in grouped] + [k for k in grouped if k not in KIND_NAMES]
        events = [e for e in s.scalars(select(Event).where(Event.parent == "").order_by(Event.date.desc(), Event.id.desc())) if org_id in (e.org_ids or [])]
        ctx = dict(
            o=o,
            wiki=wiki,
            versions=versions,
            jobs=list(s.scalars(select(Job).where(Job.org_id == org_id))),
            deals=[analytics.deal_state(s, p, today) for p in o.projects],
            grouped=grouped,
            order=order,
            conflicts=list(s.scalars(select(Conflict).where(Conflict.org_id == org_id))),
            events=events,
            as_of=today,
            projects_by_id={p.id: p for p in o.projects},
        )
        return render(request, "org.html", **ctx)


@router.get("/org/{org_id}/brief", response_class=HTMLResponse)
def org_brief(request: Request, org_id: int):
    with session() as s:
        o = s.get(Org, org_id)
        if o is None:
            return HTMLResponse("Нет такой организации", 404)
        text = qa.brief(s, o)
        return render(request, "brief.html", o=o, text=text, as_of=analytics.as_of(s))


@router.get("/people", response_class=HTMLResponse)
def people(request: Request, q: str = ""):
    with session() as s:
        rows = []
        for p in s.scalars(select(Person).order_by(Person.name)):
            hay = " ".join([p.name, *p.aliases, *[j.org.name for j in p.jobs]]).lower()
            if q and q.lower() not in hay:
                continue
            rows.append(p)
        return render(request, "people.html", rows=rows, q=q)


@router.get("/person/{pid}", response_class=HTMLResponse)
def person(request: Request, pid: int):
    with session() as s:
        p = s.get(Person, pid)
        if p is None:
            return HTMLResponse("Нет такого человека", 404)
        facts = list(s.scalars(select(Fact).where(Fact.person_id == pid).order_by(Fact.date)))
        return render(request, "person.html", p=p, facts=facts)


@router.get("/projects", response_class=HTMLResponse)
def projects(request: Request, q: str = "", stage: str = "", manager: str = "", flag: str = ""):
    with session() as s:
        today = analytics.as_of(s)
        rows = []
        for p in s.scalars(select(Project).order_by(Project.id)):
            d = analytics.deal_state(s, p, today)
            hay = " ".join([p.org.name, *p.org.aliases, p.title, p.product]).lower()
            if q and q.lower() not in hay:
                continue
            if stage and p.stage != stage:
                continue
            if manager and p.manager != manager:
                continue
            if flag == "stale" and not d.stale:
                continue
            if flag == "risk" and not d.risks:
                continue
            if flag == "open" and not d.is_open:
                continue
            if flag == "dup" and not p.duplicate_of:
                continue
            rows.append(d)
        return render(request, "projects.html", rows=rows, q=q, stage=stage, manager=manager, flag=flag, as_of=today)


@router.get("/funnel", response_class=HTMLResponse)
def funnel(request: Request):
    with session() as s:
        fns = {("Всего" if m is None else m): analytics.funnel(s, m) for m in [None, *MANAGERS]}
        mgrs = analytics.managers(s)
        return render(
            request, "funnel.html", fns=fns, mgrs=mgrs, rule=analytics.RULE_WORSE, worse=analytics.worse_manager(mgrs), as_of=analytics.as_of(s)
        )


@router.get("/events", response_class=HTMLResponse)
def events(request: Request, q: str = "", channel: str = ""):
    with session() as s:
        rows = []
        for e in s.scalars(select(Event).where(Event.parent == "").order_by(Event.date.desc(), Event.id.desc())):
            if channel and e.channel != channel:
                continue
            if q and q.lower() not in (e.text + e.summary + e.code + e.author).lower():
                continue
            rows.append(e)
        return render(request, "events.html", rows=rows, q=q, channel=channel, channels=db.CHANNELS)


@router.get("/e/{code}", response_class=HTMLResponse)
def event_page(request: Request, code: str):
    with session() as s:
        e = s.scalar(select(Event).where(Event.code == code))
        if e is None:
            return HTMLResponse(f"Нет события {html.escape(code)}", 404)
        parent = s.scalar(select(Event).where(Event.code == e.parent)) if e.parent else None
        children = list(s.scalars(select(Event).where(Event.parent == e.code).order_by(Event.id)))
        facts = list(s.scalars(select(Fact).where(Fact.event_code.in_([e.code] + [c.code for c in children]))))
        orgs_ = [s.get(Org, i) for i in (e.org_ids or (parent.org_ids if parent else []) or [])]
        return render(request, "event.html", e=e, parent=parent, children=children, facts=facts, orgs=[o for o in orgs_ if o])


@router.get("/ask", response_class=HTMLResponse)
def ask_form(request: Request, q: str = ""):
    answer = ""
    if q:
        with session() as s:
            answer = qa.answer(s, q)
    return render(request, "ask.html", q=q, answer=answer)


@router.post("/upload")
async def upload(files: list[UploadFile] | None = None, text: str = Form(""), channel: str = Form(""), date: str = Form("")):
    created = []
    for f in sorted(files or [], key=lambda x: x.filename or ""):
        if not f.filename:
            continue
        data = await f.read()
        ev, dup = ingest.create_event(f.filename, data, channel_hint=channel, date_hint=date)
        created.append(ev.code)
    if text.strip():
        ev, dup = ingest.create_event(text=text, channel_hint=channel, date_hint=date)
        created.append(ev.code)
    return RedirectResponse(f"/events?new={','.join(created)}", status_code=303)


@router.get("/api/status")
def status():
    with session() as s:
        evs = list(s.scalars(select(Event).where(Event.parent == "").order_by(Event.id)))
        return JSONResponse(
            {"busy": ingest.busy.is_set() or not ingest._q.empty(), "events": [{"code": e.code, "status": e.status, "seconds": e.seconds, "error": e.error} for e in evs]}
        )


@router.get("/health", response_class=HTMLResponse)
def health(request: Request):
    checks = {}
    try:
        with session() as s:
            s.execute(select(Org.id).limit(1))
        checks["база"] = "ок"
    except Exception as e:  # noqa: BLE001
        checks["база"] = f"ошибка: {e}"
    checks["ИИ"] = llm.health() if config.LLM_API_KEY else "ключ LLM_API_KEY не задан"
    if config.TG_TOKEN:
        info = bot.tg("getWebhookInfo") or {}
        r = info.get("result") or {}
        checks["бот"] = ("подключён" if r.get("url") else "вебхук не задан") + f", в очереди {r.get('pending_update_count', 0)}" + (f"; последняя ошибка: {r.get('last_error_message')}" if r.get("last_error_message") else "")
    else:
        checks["бот"] = "токен TELEGRAM_BOT_TOKEN не задан"
    checks["адрес сайта"] = config.PUBLIC_URL
    return render(request, "health.html", checks=checks)


@router.post("/tg")
async def telegram(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != config.TG_SECRET:
        return JSONResponse({"ok": False}, 403)
    bot.handle_update(await request.json())
    return {"ok": True}


@router.post("/reset")
def reset(code: str = Form("")):
    if not config.RESET_CODE or code != config.RESET_CODE:
        return HTMLResponse("Сброс выключен или код неверный", 403)
    db.reset_db()
    return RedirectResponse("/", status_code=303)
