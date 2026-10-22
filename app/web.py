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

# [E01], [E01, E03], [S07], [E16, 2026-09-10; S12, 2026-09-20] — коды событий и внешних источников, дата у кода — просто текст
REF_TOKEN = r"[ESU]\d+(?:[.#]\d+)?(?:,\s*\d{4}-\d{2}-\d{2})?"
CODE_RE = re.compile(rf"\[({REF_TOKEN}(?:\s*[,;]\s*{REF_TOKEN})*)\]")
ONE_CODE = re.compile(r"[ESU]\d+(?:[.#]\d+)?")

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
    "business": "Чем занимается",
    "owners": "Владельцы и руководство",
    "size": "Размер (выручка, штат)",
    "geo": "География",
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
        return "[" + ONE_CODE.sub(lambda c: f'<a class="src" href="{code_href(c.group(0))}">{c.group(0)}</a>', m.group(1)) + "]"

    return CODE_RE.sub(rep, esc)


def md(text: str) -> str:
    """Минимальный markdown: заголовки, списки, жирный, ссылки на события."""
    out, in_list = [], False
    text = re.sub(r"^```\w*\s*$", "", text or "", flags=re.M)
    for raw in text.splitlines():
        if raw.strip() == "---":
            continue
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
            links=org_links(s, o),
            history=[(KIND_NAMES[k], [f for f in facts if f.kind == k]) for k in ("budget", "deadline", "scope", "decision") if sum(1 for f in facts if f.kind == k) > 1],
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


def node_name(s, typ: str, ident: str) -> tuple[str, str]:
    """(подпись, ссылка) для конца связи графа."""
    if typ == "org" and s.get(Org, int(ident)):
        return s.get(Org, int(ident)).name, f"/org/{ident}"
    if typ == "person" and s.get(Person, int(ident)):
        return s.get(Person, int(ident)).name, f"/person/{ident}"
    if typ == "project" and s.get(Project, int(ident)):
        p = s.get(Project, int(ident))
        return f"{p.org.name}: {p.product or p.title}", f"/org/{p.org_id}"
    return ident, ""


def org_links(s, o: Org) -> list[dict]:
    from .db import Link

    mine = {("org", str(o.id))} | {("person", str(j.person_id)) for j in s.scalars(select(Job).where(Job.org_id == o.id))}
    mine |= {("project", str(p.id)) for p in o.projects}
    out = []
    for lk in s.scalars(select(Link)):
        if (lk.a_type, lk.a_id) in mine or (lk.b_type, lk.b_id) in mine:
            a, ah = node_name(s, lk.a_type, lk.a_id)
            b, bh = node_name(s, lk.b_type, lk.b_id)
            out.append({"a": a, "ah": ah, "kind": lk.kind, "b": b, "bh": bh, "event": lk.event_code, "quote": lk.quote})
    return out


@router.get("/org/{org_id}/brief", response_class=HTMLResponse)
def org_brief(request: Request, org_id: int, na: str = ""):
    with session() as s:
        o = s.get(Org, org_id)
        if o is None:
            return HTMLResponse("Нет такой организации", 404)
        day = qa.question_date(s, na, bare=True)
        text = qa.brief(s, o, day)
        return render(request, "brief.html", o=o, text=text, as_of=day)


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
        from .db import Link

        links = []
        for lk in s.scalars(select(Link).where(((Link.a_type == "person") & (Link.a_id == str(pid))) | ((Link.b_type == "person") & (Link.b_id == str(pid))))):
            a, ah = node_name(s, lk.a_type, lk.a_id)
            b, bh = node_name(s, lk.b_type, lk.b_id)
            links.append({"a": a, "ah": ah, "kind": lk.kind, "b": b, "bh": bh, "event": lk.event_code, "quote": lk.quote})
        return render(request, "person.html", p=p, facts=facts, links=links)


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
def event_page(request: Request, code: str, hl: str = ""):
    with session() as s:
        e = s.scalar(select(Event).where(Event.code == code))
        if e is None:
            return HTMLResponse(f"Нет события {html.escape(code)}", 404)
        parent = s.scalar(select(Event).where(Event.code == e.parent)) if e.parent else None
        children = list(s.scalars(select(Event).where(Event.parent == e.code).order_by(Event.id)))
        facts = list(s.scalars(select(Fact).where(Fact.event_code.in_([e.code] + [c.code for c in children]))))
        orgs_ = [s.get(Org, i) for i in (e.org_ids or (parent.org_ids if parent else []) or [])]
        text_html = html.escape(e.text or "")
        quotes = [hl] if hl else [f.quote for f in facts if f.quote]
        for q in sorted({q.strip() for q in quotes if q and len(q.strip()) > 6}, key=len, reverse=True):
            text_html = text_html.replace(html.escape(q), f'<mark id="hl">{html.escape(q)}</mark>' if q == hl else f"<mark>{html.escape(q)}</mark>")
        return render(request, "event.html", e=e, parent=parent, children=children, facts=facts, orgs=[o for o in orgs_ if o], text_html=text_html, hl=hl)


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


@router.get("/cleanup", response_class=HTMLResponse)
def cleanup(request: Request):
    """Что сделано со старой таблицей при загрузке: склеенные написания, дубли сделок, пропуски дат, причины закрытия. Без ИИ."""
    with session() as s:
        projs = list(s.scalars(select(Project).where(Project.source.like("%#%"))))
        projs.sort(key=lambda p: (p.source.split("#")[0], int(p.source.split("#")[1]) if p.source.split("#")[1].isdigit() else 0))
        raw = {}
        for e in s.scalars(select(Event).where(Event.code.in_([p.source for p in projs]))):
            m = re.search(r"клиент:\s*([^|]+)", e.text or "")
            raw[e.code] = m.group(1).strip() if m else ""
        by_id = {p.id: p for p in projs}
        rows = []
        for p in projs:
            dup = by_id.get(p.duplicate_of) if p.duplicate_of else None
            rows.append({"p": p, "raw": raw.get(p.source, ""), "dup": dup, "missing": [] if dup else analytics.missing_dates(p)})
        groups = {}
        for r in rows:
            groups.setdefault(r["p"].org_id, []).append(r)
        spellings = [
            {"org": g[0]["p"].org, "rows": g, "deals": len({r["p"].product for r in g})}
            for g in groups.values()
            if len({r["raw"] for r in g}) > 1
        ]
        mgr = {}
        for r in rows:
            if not r["dup"]:
                m = mgr.setdefault(r["p"].manager, {"deals": 0, "missing": 0})
                m["deals"] += 1
                m["missing"] += len(r["missing"])
        summary = {
            "rows": len(rows),
            "orgs": len(groups),
            "deals": sum(1 for r in rows if not r["dup"]),
            "dups": sum(1 for r in rows if r["dup"]),
            "missing": sum(len(r["missing"]) for r in rows),
        }
        return render(request, "cleanup.html", rows=rows, spellings=spellings, mgr=mgr, summary=summary)


@router.post("/recheck")
def recheck(code: str = Form(""), since: str = Form("")):
    """Повторная сверка обещаний и подтверждений (≈1 ₽ за событие), по коду сброса."""
    if not config.RESET_CODE or code != config.RESET_CODE:
        return HTMLResponse("Выключено или код неверный", 403)
    from .ingest import recheck_all

    from .ingest import parse_date

    return JSONResponse({"events": recheck_all(parse_date(since) if since else None)})


# ---------- граф (Г1, Г2) ----------


@router.get("/graph", response_class=HTMLResponse)
def graph_page(request: Request, all: int = 0):
    import json

    from . import graph as G

    with session() as s:
        g = G.build(s, include_table=bool(all))
        hidden = G.hidden_links(s, g)
        edges = sorted(g.edges, key=lambda e: (e["kind"], g.nodes[e["from"]]["label"]))
        data = {"nodes": list(g.nodes.values()), "edges": g.edges}
        return render(request, "graph.html", data=json.dumps(data, ensure_ascii=False), edges=edges, nodes=g.nodes, hidden=hidden, all=all)


# ---------- экран акционера (Е4) ----------


@router.get("/owner", response_class=HTMLResponse)
def owner(request: Request):
    with session() as s:
        today = analytics.as_of(s)
        deals = analytics.all_deals(s)
        open_ = [d for d in deals if d.is_open]
        promises = [f for f in s.scalars(select(Fact).where(Fact.kind == "promise", Fact.done.is_(False))) if not f.superseded_by]
        waiting = [f for f in s.scalars(select(Fact).where(Fact.kind == "waiting", Fact.done.is_(False))) if not f.superseded_by]
        by_stage = {st: [d for d in open_ if d.project.stage == st] for st in analytics.OPEN_STAGES}
        mgr = {m: [d for d in open_ if d.project.manager == m] for m in MANAGERS}
        ctx = dict(
            as_of=today,
            open_=open_,
            stale=[d for d in open_ if d.stale],
            risky=[d for d in open_ if d.risks],
            promises=promises,
            waiting=waiting,
            by_stage=by_stage,
            mgr=mgr,
            conflicts=list(s.scalars(select(Conflict))),
            funnel=analytics.funnel(s),
            orgs={o.id: o for o in s.scalars(select(Org))},
            mgrs=analytics.managers(s),
            worse=analytics.worse_manager(analytics.managers(s)),
            rule=analytics.RULE_WORSE,
        )
        return render(request, "owner.html", **ctx)


@router.get("/promises", response_class=HTMLResponse)
def promises_page(request: Request, kind: str = "promise", manager: str = ""):
    with session() as s:
        rows = []
        for f in s.scalars(select(Fact).where(Fact.kind == kind).order_by(Fact.date)):
            if f.superseded_by:
                continue
            o = s.get(Org, f.org_id) if f.org_id else None
            proj = s.get(Project, f.project_id) if f.project_id else None
            m = proj.manager if proj else ""
            if manager and m != manager:
                continue
            rows.append((f, o, m))
        title = {"promise": "Что мы обещали", "waiting": "Что ждём от клиентов", "request": "О чём просили клиенты"}.get(kind, kind)
        return render(request, "promises.html", rows=rows, title=title, kind=kind, manager=manager)


# ---------- выгрузка для проверки агентом ----------


@router.get("/api/dump")
def dump():
    with session() as s:
        return JSONResponse(
            {
                "as_of": str(analytics.as_of(s)),
                "events": [
                    {"code": e.code, "date": str(e.date), "channel": e.channel, "status": e.status, "error": e.error, "summary": e.summary,
                     "orgs": e.org_ids, "live": e.live_contact, "same_meeting": e.same_meeting_as, "internal": e.internal}
                    for e in s.scalars(select(Event).where(Event.parent == "").order_by(Event.id))
                ],
                "orgs": [{"id": o.id, "name": o.name, "aliases": o.aliases, "city": o.city} for o in s.scalars(select(Org))],
                "people": [
                    {"id": p.id, "name": p.name, "aliases": p.aliases, "note": p.note,
                     "jobs": [{"org": j.org.name, "role": j.role, "current": j.current, "since": str(j.since), "until": str(j.until), "ev": j.event_code} for j in p.jobs]}
                    for p in s.scalars(select(Person))
                ],
                "projects": [
                    {"id": p.id, "org": p.org.name, "product": p.product, "title": p.title, "manager": p.manager, "stage": p.stage,
                     "history": p.stage_history, "paused": p.paused, "close": p.close_reason, "dup": p.duplicate_of, "src": p.source}
                    for p in s.scalars(select(Project))
                ],
                "facts": [
                    {"org": f.org_id, "person": f.person_id, "project": f.project_id, "kind": f.kind, "text": f.text, "value": f.value, "ev": f.event_code,
                     "ch": f.channel, "ok": f.confirmed, "conf": f.confidential, "done": f.done_event, "done_q": f.done_quote, "conf_by": f.confirmed_by, "repl": f.superseded_by}
                    for f in s.scalars(select(Fact).where(~Fact.event_code.like("E00#%")))
                ],
                "conflicts": [
                    {"org": c.org_id, "subject": c.subject, "a": c.version_a, "ea": c.event_a, "b": c.version_b, "eb": c.event_b, "verdict": c.verdict}
                    for c in s.scalars(select(Conflict))
                ],
                "links": [{"a": f"{lk.a_type}:{lk.a_id}", "kind": lk.kind, "b": f"{lk.b_type}:{lk.b_id}", "ev": lk.event_code} for lk in s.scalars(select(db.Link))],
            }
        )


@router.get("/api/version")
def version():
    import os

    return {"commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7]}


@router.get("/org/{org_id}/research", response_class=HTMLResponse)
def org_research(request: Request, org_id: int, na: str = ""):
    with session() as s:
        o = s.get(Org, org_id)
        if o is None:
            return HTMLResponse("Нет такой организации", 404)
        day = qa.question_date(s, na, bare=True)
        text = qa.company_brief(s, o, day)
        return render(request, "brief.html", o=o, text=text, as_of=day, title=f"Справка о компании: {o.name}", back=f"/org/{o.id}")


@router.get("/person/{pid}/research", response_class=HTMLResponse)
def person_research(request: Request, pid: int):
    with session() as s:
        p = s.get(Person, pid)
        if p is None:
            return HTMLResponse("Нет такого человека", 404)
        text = qa.person_brief(s, p)
        return render(request, "brief.html", o=p, text=text, as_of=analytics.as_of(s), title=f"Справка о человеке: {p.name}", back=f"/person/{p.id}")
