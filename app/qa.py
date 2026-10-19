"""Разговор с базой: ответы на вопросы, справка к встрече, распознавание «вопрос или событие»."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics, config, llm, prompts
from .db import Conflict, Event, Fact, Org, Person, Project
from .ingest import org_card_json


def base_context(s: Session) -> str:
    today = analytics.as_of(s)
    lines = [f"Дата вопроса (последнее загруженное событие): {today}", ""]
    lines.append("== ОРГАНИЗАЦИИ, ПРОЕКТЫ, ЛЮДИ ==")
    for o in s.scalars(select(Org).order_by(Org.id)):
        al = f" (другие имена: {', '.join(o.aliases)})" if o.aliases else ""
        lines.append(f"# {o.name}{al} {('— ' + o.city) if o.city else ''} [орг {o.id}, первое событие {o.first_event}]")
        for p in o.projects:
            st = analytics.deal_state(s, p, today)
            flags = []
            if p.duplicate_of:
                flags.append(f"ДУБЛЬ сделки {p.duplicate_of}, не считать")
            if p.paused:
                flags.append("пауза")
            if st.stale:
                flags.append(f"ПРОТУХЛА: нет живого контакта {st.days} дн.")
            if st.risks:
                flags.append("В РИСКЕ: " + "; ".join(f"{r.text} [{r.event_code}]" for r in st.risks))
            hist = " → ".join(f"{h['stage']} {h['date']} [{h['event']}]" for h in p.stage_history or [])
            lines.append(
                f"  проект {p.id}: {p.title} / {p.product}; менеджер {p.manager or '—'}; этап «{p.stage}»; история: {hist}; "
                f"последний живой контакт {st.last_contact or 'нет'} [{st.last_contact_event}]"
                + (f"; причина закрытия: {p.close_reason}" if p.close_reason else "")
                + (f"; {'; '.join(flags)}" if flags else "")
            )
        for f in s.scalars(select(Fact).where(Fact.org_id == o.id).order_by(Fact.date, Fact.id)):
            mark = []
            if f.superseded_by:
                mark.append(f"заменено [{f.superseded_by}]")
            if not f.confirmed:
                mark.append("не подтверждено")
            if f.done:
                mark.append(f"выполнено [{f.done_event}]")
            if f.confidential:
                mark.append("конфиденциально")
            lines.append(f"  - {f.kind}: {f.text} [{f.event_code}, {f.date}, {f.channel}]" + (f" ({', '.join(mark)})" if mark else ""))
        for c in s.scalars(select(Conflict).where(Conflict.org_id == o.id)):
            lines.append(f"  ! противоречие «{c.subject}»: «{c.version_a}» [{c.event_a}] vs «{c.version_b}» [{c.event_b}] — {c.verdict}")
    lines.append("")
    lines.append("== ЛЮДИ ==")
    for p in s.scalars(select(Person).order_by(Person.id)):
        jobs = "; ".join(
            f"{j.role or 'роль не названа'} в {j.org.name} ({'сейчас' if j.current else 'раньше'}, с {j.since}{', до ' + str(j.until) if j.until else ''}) [{j.event_code}]"
            for j in p.jobs
        )
        al = f" (другие имена: {', '.join(p.aliases)})" if p.aliases else ""
        lines.append(f"- {p.name}{al}: {jobs or p.note or 'организация не названа'}")
    lines.append("")
    lines.append("== СОБЫТИЯ ==")
    for e in s.scalars(select(Event).where(Event.parent == "", Event.status == "done").order_by(Event.date, Event.id)):
        lines.append(f"[{e.code}] {e.date} {e.channel} {e.author}: {e.summary}" + (" (живой контакт)" if e.live_contact else ""))
    lines.append("")
    lines.append("== СТАРАЯ ТАБЛИЦА: ВОРОНКА И МЕНЕДЖЕРЫ ==")
    for m in [None] + list(analytics.MANAGERS):
        fn = analytics.funnel(s, m)
        lines.append(
            f"Воронка {'всего' if m is None else m}: "
            + "; ".join(f"{r['stage']} {r['count']}" + (f" (конверсия {r['conv']}%)" if r.get("conv") is not None else "") for r in fn)
        )
    rows = analytics.managers(s)
    for r in rows:
        lines.append(
            f"Менеджер {r['manager']}: сделок в таблице {r['deals']}, пропущенных дат этапов {r['missing']}, "
            f"открытых без контакта >42 дн. {r['stale']} ({', '.join(d.project.org.name for d in r['stale_deals'])})"
        )
    lines.append(analytics.RULE_WORSE + f" По правилу хуже: {analytics.worse_manager(rows)}.")
    return "\n".join(lines)


def answer(s: Session, question: str) -> str:
    ctx = base_context(s)
    return llm.chat_text(prompts.ANSWER_SYSTEM, f"БАЗА CRM:\n{ctx}\n\nВОПРОС: {question}")


def find_org_by_text(s: Session, text: str) -> Org | None:
    from .names import org_key

    t = org_key(text)
    if not t:
        return None
    best = None
    for o in s.scalars(select(Org)):
        for n in [o.name] + list(o.aliases or []):
            k = org_key(n)
            if k and (t in k or k in t):
                if best is None or len(k) > len(best[1]):
                    best = (o, k)
    return best[0] if best else None


def brief(s: Session, org: Org) -> str:
    import json

    card = org_card_json(s, org.id)
    today = analytics.as_of(s)
    card["deals"] = [
        {"project": d.project.title, "stage": d.project.stage, "last_contact": str(d.last_contact or ""), "event": d.last_contact_event,
         "stale": d.stale, "risks": [f"{r.text} [{r.event_code}]" for r in d.risks]}
        for d in (analytics.deal_state(s, p, today) for p in org.projects)
    ]
    return llm.chat_text(prompts.BRIEF_SYSTEM, f"Дата: {today}\nКАРТОЧКА (JSON):\n{json.dumps(card, ensure_ascii=False)}\n\n{prompts.BRIEF_FORMAT}")


def is_question(text: str) -> bool:
    t = text.strip()
    if len(t) > 600 or "\n---" in t or t.startswith("---"):
        return False
    if t.endswith("?") or re.match(r"^(кто|что|где|когда|сколько|какие|какой|какая|как|почему|зачем|есть ли|покажи|дай|назови|чем)\b", t.lower()):
        return True
    try:
        return llm.chat_text(prompts.CLASSIFY_SYSTEM, t, model=config.LLM_MODEL_FAST).lower().startswith("q")
    except Exception:  # noqa: BLE001
        return len(t) < 200


CODE = r"[EU]\d+(?:[.#]\d+)?"


def codes_in(text: str) -> list[str]:
    """Коды событий из ссылок вида [E05] и [E03, E04]."""
    out: list[str] = []
    for grp in re.findall(r"\[([^\]]+)\]", text or ""):
        out += re.findall(CODE, grp)
    return list(dict.fromkeys(out))


__all__ = ["answer", "brief", "find_org_by_text", "is_question", "codes_in", "base_context", "Project"]
