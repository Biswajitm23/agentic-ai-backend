"""Tiny forward-only schema upkeep, run at startup after ``create_all``.

``create_all`` only creates missing tables, so columns added to an existing table
would be silently absent on a database that already has data (the Railway
Postgres, a developer's SQLite file). This adds them. Anything more involved
belongs in Alembic; for now the app only ever *adds* nullable/defaulted columns.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings

logger = logging.getLogger(__name__)

# table -> column -> DDL type + default (SQL fragments, no user input)
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "products": {
        "vendor": "VARCHAR(200)",
        "status": "VARCHAR(20) DEFAULT 'active'",
        "product_title": "VARCHAR(200)",
        "variant_title": "VARCHAR(200)",
        "shopify_product_id": "VARCHAR(64)",
        "shopify_variant_id": "VARCHAR(64)",
        "handle": "VARCHAR(255)",
        "has_cost": "BOOLEAN DEFAULT TRUE",
        "source": "VARCHAR(16) DEFAULT 'seed'",
    },
    "campaigns": {
        "attribution": "VARCHAR(32)",
        "attribution_key": "VARCHAR(200)",
        "first_order_at": "TIMESTAMP",
        "last_order_at": "TIMESTAMP",
        "source": "VARCHAR(16) DEFAULT 'seed'",
    },
    "orders": {
        "financial_status": "VARCHAR(30)",
        "channel": "VARCHAR(50)",
        "subtotal": "FLOAT DEFAULT 0",
        "tax": "FLOAT DEFAULT 0",
        "shipping": "FLOAT DEFAULT 0",
        "discounts": "FLOAT DEFAULT 0",
        "refunded": "FLOAT DEFAULT 0",
        "payment_fees": "FLOAT DEFAULT 0",
        "cogs": "FLOAT DEFAULT 0",
        "item_count": "INTEGER DEFAULT 0",
        "discount_code": "VARCHAR(100)",
        "utm_campaign": "VARCHAR(200)",
        "utm_source": "VARCHAR(100)",
        "utm_medium": "VARCHAR(100)",
        "shopify_order_id": "VARCHAR(64)",
        "source": "VARCHAR(16) DEFAULT 'seed'",
    },
    "ops_tasks": {
        "domain": "VARCHAR(20) DEFAULT 'operations'",
        "source": "VARCHAR(16) DEFAULT 'seed'",
    },
    "expenses": {
        "order_number": "VARCHAR(32)",
        "source": "VARCHAR(16) DEFAULT 'seed'",
    },
}


def _existing_columns(sync_conn) -> dict[str, set[str]]:
    insp = inspect(sync_conn)
    return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}


async def ensure_columns(conn: AsyncConnection) -> None:
    existing = await conn.run_sync(_existing_columns)
    for table, columns in ADDED_COLUMNS.items():
        have = existing.get(table)
        if have is None:
            continue  # create_all makes new tables with every column already
        for column, ddl in columns.items():
            if column in have:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info("Added column %s.%s", table, column)


async def ensure_vector_index(conn: AsyncConnection) -> None:
    """An HNSW cosine index on the knowledge embeddings (Postgres + pgvector only)."""
    if not settings.is_postgres:
        return
    try:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
    except Exception:
        logger.warning("Could not create the pgvector index; searches will scan", exc_info=True)
