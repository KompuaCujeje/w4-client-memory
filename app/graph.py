"""Граф связей: организации, люди, проекты, темы, продукты, новости. У каждой связи — событие-источник.

Семь видов связей (атом Г1): работает в, работал в, участвует в проекте, знаком с, интересуется темой,
предложен продукт, новость касается. Плюс «неочевидные связи» — цепочки через людей между разными клиентами.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import CLOSED, Event, Job, Link, Org, Person, Project

TOPICS = ["интеграция с 1С", "чат-боты", "мобильные приложения", "автоматизация продаж", "смена руководства", "расширение бизнеса"]
PRODUCT_TOPIC = {
    "мобильное приложение": "мобильные приложения",
    "чат-бот": "чат-боты",
    "интеграция с 1С": "интеграция с 1С",
    "внедрение CRM": "автоматизация продаж",
    "ИИ-ассистент": "чат-боты",
}
KNOW = {"знаком с", "учился с", "однокурсник", "друг", "коллега", "рекомендовал"}


@dataclass
class Graph:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)

    def node(self, key: str, label: str, group: str, href: str = "") -> str:
        if key not in self.nodes:
            self.nodes[key] = {"id": key, "label": label, "group": group, "href": href}
        return key

    def edge(self, a: str, b: str, kind: str, event: str, quote: str = "") -> None:
        for e in self.edges:
            if e["from"] == a and e["to"] == b and e["kind"] == kind:
                if event and event not in e["events"]:
                    e["events"].append(event)
                return
        self.edges.append({"from": a, "to": b, "kind": kind, "events": [event] if event else [], "quote": quote})


def _org(g: Graph, o: Org) -> str:
    return g.node(f"org{o.id}", o.name, "org", f"/org/{o.id}")


def _person(g: Graph, p: Person) -> str:
    return g.node(f"person{p.id}", p.name, "person", f"/person/{p.id}")


def _project(g: Graph, p: Project) -> str:
    return g.node(f"project{p.id}", f"{p.product or p.title}", "project", f"/org/{p.org_id}")


def _resolve(g: Graph, s: Session, typ: str, ident: str) -> str | None:
    try:
        if typ == "org":
            o = s.get(Org, int(ident))
            return _org(g, o) if o else None
        if typ == "person":
            p = s.get(Person, int(ident))
            return _person(g, p) if p else None
        if typ == "project":
            p = s.get(Project, int(ident))
            return _project(g, p) if p else None
    except (TypeError, ValueError):
        return None
    text = (ident or "").strip()
    low = text.lower()
    if low.startswith("тема:") or low in TOPICS:
        t = text.split(":", 1)[-1].strip()
        return g.node(f"topic:{t.lower()}", t, "topic")
    if low.startswith("продукт:"):
        t = text.split(":", 1)[-1].strip()
        return g.node(f"product:{t.lower()}", t, "product")
    return g.node(f"text:{low}", text, "other") if text else None


def build(s: Session, include_table: bool = False) -> Graph:
    """include_table=False — без 25 лидов старой таблицы, у которых нет событий (иначе граф нечитаем)."""
    g = Graph()
    event_orgs: set[int] = set()
    for e in s.scalars(select(Event).where(Event.parent == "", Event.channel != "таблица")):
        event_orgs |= set(e.org_ids or [])

    def wanted(org_id: int) -> bool:
        return include_table or org_id in event_orgs

    for j in s.scalars(select(Job)):
        if not wanted(j.org_id):
            continue
        a, b = _person(g, j.person), _org(g, j.org)
        member = any(w in (j.role or "").lower() for w in ("член совета", "председатель совета", "член ассоциации"))
        g.edge(a, b, "состоит в" if member else ("работает в" if j.current else "работал в"), j.event_code)

    for p in s.scalars(select(Project).where(Project.duplicate_of.is_(None))):
        if not wanted(p.org_id):
            continue
        o, pr = _org(g, p.org), _project(g, p)
        first = (p.stage_history or [{}])[0].get("event") or p.source
        g.edge(o, pr, "проект", first)
        if p.product:
            g.edge(o, g.node(f"product:{p.product.lower()}", p.product, "product"), "предложен продукт", first)
            topic = PRODUCT_TOPIC.get(p.product)
            if topic:
                g.edge(o, g.node(f"topic:{topic.lower()}", topic, "topic"), "интересуется темой", first)
        if p.stage != CLOSED:
            for j in s.scalars(select(Job).where(Job.org_id == p.org_id, Job.current.is_(True))):
                g.edge(_person(g, j.person), pr, "участвует в проекте", j.event_code)

    for lk in s.scalars(select(Link)):
        a = _resolve(g, s, lk.a_type, lk.a_id)
        b = _resolve(g, s, lk.b_type, lk.b_id)
        if a and b and a != b:
            kind = lk.kind
            if kind not in ("работает в", "работал в", "участвует в проекте", "интересуется темой", "предложен продукт", "новость касается"):
                kind = "знаком с" if any(k in kind for k in KNOW) else kind
            g.edge(a, b, kind, lk.event_code, lk.quote)

    for e in s.scalars(select(Event).where(Event.parent == "", Event.channel == "новость")):
        n = g.node(f"news:{e.code}", f"новость {e.code}", "news", f"/e/{e.code}")
        for oid in e.org_ids or []:
            o = s.get(Org, oid)
            if o:
                g.edge(n, _org(g, o), "новость касается", e.code)
    return g


def hidden_links(s: Session, g: Graph) -> list[dict]:
    """Неочевидные связи: знакомство людей из РАЗНЫХ организаций, человек в двух организациях,
    организация с несколькими именами. Каждая — со ссылками на события."""
    out = []
    person_orgs: dict[str, list[tuple[str, str, str]]] = {}
    for e in g.edges:
        if e["kind"] in ("работает в", "работал в", "состоит в") and e["from"].startswith("person"):
            person_orgs.setdefault(e["from"], []).append((e["to"], e["kind"], ",".join(e["events"])))
    label = {k: v["label"] for k, v in g.nodes.items()}
    clients = {e["from"] for e in g.edges if e["kind"] == "проект"}  # наши клиенты — организации со сделками
    for e in g.edges:
        if e["kind"] == "знаком с" or "учил" in e["kind"]:
            a, b = e["from"], e["to"]
            oa = [x for x in person_orgs.get(a, []) if x[1] != "состоит в" and x[0] in clients]
            ob = [y for y in person_orgs.get(b, []) if y[1] != "состоит в" and y[0] in clients]
            for x in oa:
                for y in ob:
                    if x[0] != y[0]:
                        out.append(
                            {
                                "text": f"{label[a]} ({label[x[0]]}) {e['kind']} {label[b]} ({label[y[0]]}) — тёплый вход из «{label[x[0]]}» в «{label[y[0]]}»",
                                "events": sorted(set(e["events"] + x[2].split(",") + y[2].split(","))),
                            }
                        )
    # люди разных клиентов, которые состоят в одной третьей организации (совет ассоциации и т. п.)
    members: dict[str, list[tuple[str, str]]] = {}
    for p, orgs in person_orgs.items():
        for o, k, ev in orgs:
            if k == "состоит в":
                members.setdefault(o, []).append((p, ev))
    for x, ps in members.items():
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = ps[i][0], ps[j][0]
                wa = [o for o, k, _ in person_orgs.get(a, []) if k != "состоит в" and o != x and o in clients]
                wb = [o for o, k, _ in person_orgs.get(b, []) if k != "состоит в" and o != x and o in clients]
                for oa in wa:
                    for ob in wb:
                        if oa != ob:
                            evs = {c for _, ev in (ps[i], ps[j]) for c in ev.split(",")}
                            evs |= {c for o, _, ev in person_orgs.get(a, []) + person_orgs.get(b, []) if o in (oa, ob) for c in ev.split(",")}
                            out.append(
                                {
                                    "text": f"{label[a]} ({label[oa]}) и {label[b]} ({label[ob]}) — оба в {label[x] if "«" in label[x] else "«" + label[x] + "»"}: общий круг, повод для знакомства «{label[oa]}» и «{label[ob]}»",
                                    "events": sorted(c for c in evs if c),
                                }
                            )
    for p, orgs in person_orgs.items():
        names = {o for o, _, _ in orgs}
        if len(names) > 1 and names & clients:
            out.append(
                {
                    "text": f"{label[p]} связан с несколькими организациями: " + ", ".join(f"{label[o]} ({k})" for o, k, _ in orgs),
                    "events": sorted({c for _, _, ev in orgs for c in ev.split(",")}),
                }
            )
    return out
