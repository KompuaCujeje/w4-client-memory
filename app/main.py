"""Точка входа: uvicorn app.main:app"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import bot, db, ingest
from .web import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    ingest.start_worker()
    try:
        log.info("webhook: %s", bot.set_webhook())
    except Exception:  # noqa: BLE001
        log.exception("webhook setup failed")
    yield


app = FastAPI(title="Северный мост · CRM", lifespan=lifespan)
app.include_router(router)
