"""Счёт по правилам словаря: последний живой контакт, протухшие сделки, риски, воронка, работа менеджеров."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import CLOSED, MANAGERS, OPEN_STAGES, STAGES, Event, Fact, Job, Project

STALE_DAYS = 42


def as_of(s: Session) -> dt.date:
    """Дата вопроса = дата последнего загруженного события."""
    d = s.scalar(select(func.max(Event.date)).where(Event.parent == "", Event.status == "done"))
    return d or dt.date.today()


@dataclass
class LeaderRisk:
    """Риск без отдельного факта: ушёл директор/собственник (по связи человека с организацией)."""

    id: int
    date: dt.date
    event_code: str
    text: str
    superseded_by: str = ""


@dataclass
class DealState:
    project: Project
    last_contact: dt.date | None
    last_contact_event: str
    days: int | None
    stale: bool
    risks: list[Fact] = field(default_factory=list)
    is_open: bool = False
    cleared: dict = field(default_factory=dict)  # fact_id риска → (этап, код события), которое его сняло
    past: list = field(default_factory=list)  # снятые риски: (риск, этап, событие, дата)
    ignored: set = field(default_factory=set)  # факты «риск», которые по словарю не риск (сезон, новость рынка)


# словарь: риск — сменился человек, принимающий решение; урезали или заморозили бюджет; клиент поставил проект на паузу
RISK_RULE_RE = re.compile(r"урез|сократил|сокращ.{0,10}бюджет|бюджет.{0,30}(сниж|замороз|сократ)|заморож|замороз|пауз|приостанов|сменил.{0,20}(директор|руковод)|уш[её]л", re.I)
RULE_RISK = "Риск по словарю: сменился человек, принимающий решение, урезали или заморозили бюджет, клиент поставил проект на паузу; снимается, когда следующий живой контакт двигает сделку на следующий этап. Закрытая сделка — не риск."


def live_contacts(s: Session, org_id: int) -> list[Event]:
    evs = s.scalars(select(Event).where(Event.parent == "", Event.status == "done", Event.live_contact.is_(True)))
    return sorted([e for e in evs if org_id in (e.org_ids or []) and e.date], key=lambda e: e.date)


def deal_state(s: Session, p: Project, today: dt.date | None = None) -> DealState:
    today = today or as_of(s)
    last, last_ev = None, ""
    # для строк старой таблицы последний контакт — последняя дата в строке
    # строка-дубль той же сделки тоже говорит о контакте с клиентом
    rows = [p] + list(s.scalars(select(Project).where(Project.duplicate_of == p.id)))
    for r in rows:
        if r.source and "#" in r.source and r.stage_dates:
            d = max(dt.date.fromisoformat(v) for v in r.stage_dates.values())
            if last is None or d > last:
                last, last_ev = d, r.source
    for e in live_contacts(s, p.org_id):
        if e.date <= today and (last is None or e.date >= last):
            last, last_ev = e.date, e.code
    is_open = p.stage in OPEN_STAGES and p.duplicate_of is None
    days = (today - last).days if last else None
    stale = bool(is_open and (days is None or days > STALE_DAYS))
    risks, cleared, past, ignored = [], {}, [], set()
    if is_open:
        for f in s.scalars(select(Fact).where(Fact.org_id == p.org_id, Fact.kind == "risk", Fact.superseded_by == "")):
            if f.project_id not in (None, p.id):
                continue
            if not RISK_RULE_RE.search(f.text or "") or (f.date and f.date > today):
                ignored.add(f.id)
                continue
            # риск снимается, когда следующий живой контакт двигает сделку на следующий этап
            moved = [h for h in (p.stage_history or [])[1:] if h.get("date", "") > str(f.date)]
            if moved:
                cleared[f.id] = (moved[0].get("stage", ""), moved[0].get("event", ""))
                past.append((f, moved[0].get("stage", ""), moved[0].get("event", ""), moved[0].get("date", "")))
            else:
                risks.append(f)
        # словарь: «сменился человек, принимающий решение» — риск с даты события, из которого это известно
        start = (p.stage_history or [{}])[0].get("date", "")
        for j in s.scalars(select(Job).where(Job.org_id == p.org_id, Job.current.is_(False))):
            role = (j.role or "").lower()
            if not j.until or not any(w in role for w in ("директор", "собственник", "руководител", "решени")):
                continue
            if str(j.until) < start or j.until > today:
                continue
            r = LeaderRisk(
                id=-j.id,
                date=j.until,
                event_code=j.until_event or j.event_code,
                text=f"сменился человек, принимающий решение: {j.person.name} ушёл",
            )
            moved = [h for h in (p.stage_history or [])[1:] if h.get("date", "") > str(j.until)]
            if moved:
                cleared[r.id] = (moved[0].get("stage", ""), moved[0].get("event", ""))
                past.append((r, moved[0].get("stage", ""), moved[0].get("event", ""), moved[0].get("date", "")))
            else:
                risks.append(r)
    risks.sort(key=lambda r: r.date or dt.date.min)
    return DealState(p, last, last_ev, days, stale, risks, is_open, cleared, past, ignored)


def all_deals(s: Session) -> list[DealState]:
    today = as_of(s)
    return [deal_state(s, p, today) for p in s.scalars(select(Project).order_by(Project.id))]


# ---------- старая таблица: воронка и менеджеры ----------


def _table_leads(s: Session) -> list[Project]:
    return list(s.scalars(select(Project).where(Project.source.like("%#%"), Project.duplicate_of.is_(None)).order_by(Project.id)))


def reached(p: Project) -> list[str]:
    """До каких этапов лид дошёл (правило словаря)."""
    dates = p.stage_dates or {}
    dated = [i for i, st in enumerate(STAGES) if st in dates]
    if p.stage == CLOSED:
        top = max(dated) if dated else 0
    else:
        cur = STAGES.index(p.stage) if p.stage in STAGES else 0
        top = max([cur] + dated)
    return STAGES[: top + 1]


def missing_dates(p: Project) -> list[str]:
    dates = p.stage_dates or {}
    return [st for st in reached(p) if st != "в работе" and st not in dates]


def funnel(s: Session, manager: str | None = None) -> list[dict]:
    leads = [p for p in _table_leads(s) if manager is None or p.manager == manager]
    counts = [sum(1 for p in leads if st in reached(p)) for st in STAGES]
    out = []
    for i, st in enumerate(STAGES):
        row = {"stage": st, "count": counts[i], "ids": [p.id for p in leads if st in reached(p)]}
        if i > 0:
            prev = counts[i - 1]
            row["conv"] = round(100 * counts[i] / prev) if prev else None
        out.append(row)
    return out


def managers(s: Session, today: dt.date | None = None) -> list[dict]:
    today = today or as_of(s)
    leads = _table_leads(s)
    out = []
    for m in MANAGERS:
        mine = [p for p in leads if p.manager == m]
        miss = [(p, missing_dates(p)) for p in mine]
        stale = [d for d in (deal_state(s, p, today) for p in s.scalars(select(Project).where(Project.manager == m))) if d.stale]
        out.append(
            {
                "manager": m,
                "deals": len(mine),
                "deal_ids": [p.id for p in mine],
                "missing": sum(len(x) for _, x in miss),
                "missing_rows": [(p, x) for p, x in miss if x],
                "stale": len(stale),
                "stale_deals": stale,
            }
        )
    return out


RULE_WORSE = (
    "Правило «кто хуже»: больше открытых сделок без живого контакта больше 42 дней; при равенстве — больше пропущенных "
    "дат этапов на одну сделку."
)


def worse_manager(rows: list[dict]) -> str:
    def key(r):
        return (r["stale"], r["missing"] / r["deals"] if r["deals"] else 0)

    return max(rows, key=key)["manager"] if rows else ""
