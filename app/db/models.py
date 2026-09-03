from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base

# Where a row came from. Rows Shopify owns are replaced wholesale on every sync;
# "seed" rows are demo data used only while no store is connected; "manual" rows
# are never touched by a sync.
SOURCE_SEED = "seed"
SOURCE_SHOPIFY = "shopify"
SOURCE_MANUAL = "manual"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    category: Mapped[str] = mapped_column(String(100))
    price: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, default=10)
    # Shopify detail (null for seeded rows)
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|draft|archived
    product_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    variant_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shopify_product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shopify_variant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    has_cost: Mapped[bool] = mapped_column(default=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_SEED)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="active")
    budget: Mapped[float] = mapped_column(Float)
    spend: Mapped[float] = mapped_column(Float, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0)
    # How the campaign was identified in Shopify: utm_campaign, discount_code, or channel
    attribution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attribution_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    first_order_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_order_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_SEED)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(32), unique=True)
    customer_name: Mapped[str] = mapped_column(String(200))
    total: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending|processing|fulfilled|cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Shopify financial detail (all in shop currency)
    financial_status: Mapped[str | None] = mapped_column(String(30), nullable=True)  # paid|pending|refunded|...
    channel: Mapped[str | None] = mapped_column(String(50), nullable=True)  # web|pos|draft_order|...
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    tax: Mapped[float] = mapped_column(Float, default=0)
    shipping: Mapped[float] = mapped_column(Float, default=0)
    discounts: Mapped[float] = mapped_column(Float, default=0)
    refunded: Mapped[float] = mapped_column(Float, default=0)
    payment_fees: Mapped[float] = mapped_column(Float, default=0)
    cogs: Mapped[float] = mapped_column(Float, default=0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    discount_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(200), nullable=True)
    utm_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_SEED)


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)


class OpsTask(Base):
    __tablename__ = "ops_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300))
    priority: Mapped[str] = mapped_column(String(10), default="medium")  # low|medium|high
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|in_progress|done
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    domain: Mapped[str] = mapped_column(String(20), default="operations")
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_SEED)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(300))
    amount: Mapped[float] = mapped_column(Float)
    expense_date: Mapped[date] = mapped_column(Date)
    order_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_SEED)


class StoreSetting(Base):
    """Small key/value facts about the connected store: currency, last sync, scopes."""

    __tablename__ = "store_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(10))  # user|assistant
    content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OrderChangeRequest(Base):
    """A cancellation or address change a shopper has started but not confirmed.

    Keyed by the chat session rather than by a token the agent carries, because
    the agent cannot carry one: only the user and assistant turns are replayed
    into its context, so a tool result minted three turns ago is long gone by the
    time the shopper says "yes". Keying it server-side also means the secret
    never enters the model's context at all.

    In the database rather than in memory so it survives a redeploy - Railway
    restarts on every push, and an in-process dict loses every pending change.

    One row per session: starting a new change replaces the old one.
    """

    __tablename__ = "order_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    order_gid: Mapped[str] = mapped_column(String(128))
    order_name: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(20))  # cancel|change_address
    email: Mapped[str] = mapped_column(String(255))

    challenge_kind: Mapped[str] = mapped_column(String(16))  # postcode|total
    # SHA-256 of the normalised answer. A postcode is order PII, and nothing here
    # needs to read it back - only compare against it.
    challenge_hash: Mapped[str] = mapped_column(String(64))

    current_address: Mapped[dict] = mapped_column(JSON, default=dict)
    order_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _embedding_column_type():
    """pgvector's Vector on Postgres; a JSON-text column on SQLite so local
    development works without the extension (see services.rag)."""
    if settings.is_postgres:
        from pgvector.sqlalchemy import Vector

        return Vector(settings.EMBEDDING_DIMENSIONS)
    return Text


class KnowledgeChunk(Base):
    """One embedded piece of knowledge the admin agent can retrieve.

    ``kind`` is a store record type (product, order, campaign, expense, task) or
    ``chat`` for a remembered admin conversation turn. Store chunks are rebuilt on
    every Shopify sync; chat chunks accumulate as the agent is used.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_kind_ref", "kind", "ref_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding = mapped_column(_embedding_column_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
