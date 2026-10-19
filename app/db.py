"""База: события, три реестра (организации, люди, проекты), факты со ссылкой на событие, вики с версиями."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from . import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# Глубина источника по словарю: от глубокого к поверхностному
CHANNEL_DEPTH = {
    "расшифровка встречи": 5,
    "письмо": 4,
    "переписка": 3,
    "голосовая": 2,
    "записка": 1,
    "новость": 0,
    "таблица": 0,
    "другое": 0,
}
CHANNELS = list(CHANNEL_DEPTH)

STAGES = ["знакомство", "потребность", "оценка и предложение", "согласование", "договор", "в работе"]
CLOSED = "закрыт без сделки"
OPEN_STAGES = STAGES[:4]
PRODUCTS = ["сайт", "мобильное приложение", "чат-бот", "внедрение CRM", "ИИ-ассистент", "интеграция с 1С", "поддержка"]
MANAGERS = ["Андрей", "Ирина"]


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # E01, U003
    date: Mapped[dt.date | None] = mapped_column(Date)
    channel: Mapped[str] = mapped_column(String(40), default="другое")
    author: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    filename: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    is_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|done|error
    error: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    org_ids: Mapped[list] = mapped_column(JSON, default=list)
    live_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    contact_kind: Mapped[str] = mapped_column(String(40), default="")
    internal: Mapped[bool] = mapped_column(Boolean, default=False)
    same_meeting_as: Mapped[str] = mapped_column(String(32), default="")
    parent: Mapped[str] = mapped_column(String(32), default="")  # сообщение чата / строка таблицы → код родителя
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    seconds: Mapped[float | None] = mapped_column(Float)
    chat_id: Mapped[str] = mapped_column(String(40), default="")  # куда сообщить о разборе

    @property
    def depth(self) -> int:
        return CHANNEL_DEPTH.get(self.channel, 0)


class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    city: Mapped[str] = mapped_column(String(200), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    first_event: Mapped[str] = mapped_column(String(32), default="")
    projects: Mapped[list[Project]] = relationship(back_populates="org", order_by="Project.id")


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    first_event: Mapped[str] = mapped_column(String(32), default="")
    jobs: Mapped[list[Job]] = relationship(back_populates="person", order_by="Job.id")


class Job(Base):
    """Связь человек — организация: роль и период. Ушёл — связь прошлая, не удаляется."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"))
    role: Mapped[str] = mapped_column(String(300), default="")
    since: Mapped[dt.date | None] = mapped_column(Date)
    until: Mapped[dt.date | None] = mapped_column(Date)
    current: Mapped[bool] = mapped_column(Boolean, default=True)
    event_code: Mapped[str] = mapped_column(String(32), default="")
    person: Mapped[Person] = relationship(back_populates="jobs")
    org: Mapped[Org] = relationship()


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"))
    title: Mapped[str] = mapped_column(String(300), default="")
    product: Mapped[str] = mapped_column(String(60), default="")
    manager: Mapped[str] = mapped_column(String(60), default="")
    stage: Mapped[str] = mapped_column(String(40), default="знакомство")
    stage_dates: Mapped[dict] = mapped_column(JSON, default=dict)  # этап -> дата ISO
    stage_history: Mapped[list] = mapped_column(JSON, default=list)  # [{stage,date,event}]
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    close_reason: Mapped[str] = mapped_column(Text, default="")
    created: Mapped[dt.date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32), default="")  # E00#5 или E01
    duplicate_of: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="")
    org: Mapped[Org] = relationship(back_populates="projects")


class Fact(Base):
    """Один факт = одна ссылка на событие. Kind — что это: бюджет, срок, обещание, просьба, личное…"""

    __tablename__ = "facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(String(40), default="other")
    text: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[str] = mapped_column(String(300), default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    event_code: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[dt.date | None] = mapped_column(Date)
    channel: Mapped[str] = mapped_column(String(40), default="")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)  # из голосовой/записки — false
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False)  # для обещаний и просьб
    done_event: Mapped[str] = mapped_column(String(32), default="")
    superseded_by: Mapped[str] = mapped_column(String(32), default="")


class Conflict(Base):
    """Противоречие источников: обе версии со ссылками, не решаем молча."""

    __tablename__ = "conflicts"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), index=True)
    subject: Mapped[str] = mapped_column(String(300), default="")
    version_a: Mapped[str] = mapped_column(Text, default="")
    event_a: Mapped[str] = mapped_column(String(32), default="")
    version_b: Mapped[str] = mapped_column(Text, default="")
    event_b: Mapped[str] = mapped_column(String(32), default="")
    verdict: Mapped[str] = mapped_column(Text, default="")  # чему верим и почему, или «не решено»
    date: Mapped[dt.date | None] = mapped_column(Date)


class Link(Base):
    """Связь для графа: работает в, знаком с, интересуется темой, предложен продукт, новость касается…"""

    __tablename__ = "links"
    id: Mapped[int] = mapped_column(primary_key=True)
    a_type: Mapped[str] = mapped_column(String(20))
    a_id: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(60))
    b_type: Mapped[str] = mapped_column(String(20))
    b_id: Mapped[str] = mapped_column(String(300))
    event_code: Mapped[str] = mapped_column(String(32), default="")
    quote: Mapped[str] = mapped_column(Text, default="")


class Wiki(Base):
    __tablename__ = "wiki"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text, default="")
    changes: Mapped[str] = mapped_column(Text, default="")
    event_code: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


def init_db() -> None:
    Base.metadata.create_all(engine)


def reset_db() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def session() -> Session:
    return SessionLocal()


def latest_wiki(s: Session, org_id: int) -> Wiki | None:
    return s.scalars(select(Wiki).where(Wiki.org_id == org_id).order_by(Wiki.version.desc())).first()
