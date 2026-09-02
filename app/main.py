import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.db import models  # noqa: F401  (register models on Base.metadata)
from app.db.base import Base
from app.db.seed import seed_if_empty
from app.db.session import AsyncSessionLocal, engine
from app.services import shopify_sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as session:
        await seed_if_empty(session, include_store_data=not shopify_sync.is_configured())
    if shopify_sync.is_configured():
        try:
            async with AsyncSessionLocal() as session:
                result = await shopify_sync.sync_shopify(session)
                logging.getLogger(__name__).info("Shopify sync on startup: %s", result)
        except Exception:
            logging.getLogger(__name__).exception("Shopify sync on startup failed; using local data")
    yield


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
