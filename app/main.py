import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import settings
from app.db import models  # noqa: F401  (register models on Base.metadata)
from app.db.base import Base
from app.db.migrate import ensure_columns, ensure_vector_index
from app.db.seed import seed_if_empty
from app.db.session import AsyncSessionLocal, engine
from app.services import handbook, insights, rag, shopify_sync


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def enable_pgvector() -> None:
    """Make the pgvector extension available, in its own transaction.

    Runs before create_all so a table may declare a Vector column. A failure is
    logged rather than fatal — the rest of the API works without vector search,
    and keeping it in a separate transaction stops a failure here from aborting
    table creation on Postgres.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("pgvector extension is available")
    except Exception:
        logger.warning("Could not enable the pgvector extension", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_postgres:
        await enable_pgvector()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_columns(conn)
        await ensure_vector_index(conn)

    connected = shopify_sync.is_configured()
    async with AsyncSessionLocal() as session:
        await seed_if_empty(session, include_store_data=not connected)

    synced = False
    if connected:
        try:
            async with AsyncSessionLocal() as session:
                result = await shopify_sync.sync_shopify(session)
                logger.info("Shopify sync on startup: %s", result)
                synced = True
        except Exception:
            logger.exception("Shopify sync on startup failed; using the data already in the database")
    if not synced:
        # The sync rebuilds the retrieval index itself; otherwise make sure the
        # agent can search whatever is in the database.
        try:
            async with AsyncSessionLocal() as session:
                await rag.rebuild_store_knowledge(session, await insights.currency(session))
        except Exception:
            logger.exception("Could not build the retrieval index")
    # The store handbook is versioned with the code, so re-embed it whenever the
    # file has changed. Unchanged, this costs one hash and no embeddings.
    try:
        async with AsyncSessionLocal() as session:
            logger.info("Handbook index: %s", await handbook.reindex(session))
    except Exception:
        logger.exception("Could not index the store handbook")

    logger.info(
        "Retrieval backend: %s (%s)",
        "pgvector" if settings.is_postgres else "SQLite JSON fallback",
        settings.embedding_provider,
    )
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
