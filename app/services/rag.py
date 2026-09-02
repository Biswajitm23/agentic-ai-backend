"""Retrieval-augmented memory for the admin agent, on pgvector.

Two kinds of knowledge live in ``knowledge_chunks``:

* **Store records** - one chunk per product variant, order, campaign, expense and
  task, written from the database after every Shopify sync (and at startup).
  They let the agent pull the handful of records relevant to a question instead
  of the whole catalogue.
* **Chat memory** - one chunk per admin conversation turn (question + answer),
  so the agent can recall what staff asked and were told earlier, across
  sessions.

On Postgres the similarity search is a pgvector cosine-distance query (with an
HNSW index, see ``db.migrate``). On SQLite - local development only - vectors
are stored as JSON and ranked in Python, which is fine for a small store.
"""

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Campaign, Expense, KnowledgeChunk, OpsTask, Order, OrderLine, Product
from app.services import embeddings

logger = logging.getLogger(__name__)

CHAT_KIND = "chat"
STORE_KINDS = ("product", "order", "campaign", "expense", "task")
MAX_CHAT_CHUNK_CHARS = 1500


@dataclass
class Hit:
    kind: str
    content: str
    meta: dict
    score: float  # cosine similarity, higher is better
    session_id: str | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Turning rows into text
# --------------------------------------------------------------------------- #


def _money(amount: float | None, currency: str) -> str:
    return f"{(amount or 0):,.2f} {currency}"


def product_text(p: Product, currency: str) -> str:
    stock_state = "OUT OF STOCK" if p.stock_qty <= 0 else ("LOW STOCK" if p.stock_qty <= p.reorder_level else "in stock")
    cost = _money(p.cost, currency) if p.has_cost else "unknown (no unit cost in Shopify)"
    return (
        f"Product: {p.name}. SKU {p.sku}. Category: {p.category}. Vendor: {p.vendor or 'n/a'}. "
        f"Status: {p.status}. Price {_money(p.price, currency)}, unit cost {cost}. "
        f"Stock {p.stock_qty} units (reorder level {p.reorder_level}) - {stock_state}."
    )


def order_text(o: Order, lines: Sequence[OrderLine], currency: str) -> str:
    items = "; ".join(f"{ln.quantity} x {ln.title}" + (f" [{ln.sku}]" if ln.sku else "") for ln in lines) or "n/a"
    attribution = []
    if o.utm_campaign:
        attribution.append(f"utm campaign '{o.utm_campaign}'")
    if o.utm_source or o.utm_medium:
        attribution.append(f"source/medium {o.utm_source or '?'}/{o.utm_medium or '?'}")
    if o.discount_code:
        attribution.append(f"discount code {o.discount_code}")
    when = o.created_at.strftime("%Y-%m-%d") if o.created_at else "unknown date"
    return (
        f"Order {o.order_number} placed {when} via {o.channel or 'store'}. Customer: {o.customer_name}. "
        f"Fulfillment: {o.status}; payment: {o.financial_status or 'n/a'}. "
        f"Total {_money(o.total, currency)} (subtotal {_money(o.subtotal, currency)}, tax {_money(o.tax, currency)}, "
        f"shipping {_money(o.shipping, currency)}, discounts {_money(o.discounts, currency)}, refunded {_money(o.refunded, currency)}). "
        f"Cost of goods {_money(o.cogs, currency)}. Items ({o.item_count}): {items}. "
        f"Marketing attribution: {', '.join(attribution) or 'none (direct)'}."
    )


def campaign_text(c: Campaign, currency: str) -> str:
    roas = f"{c.revenue / c.spend:.2f}x" if c.spend else "n/a (no spend recorded)"
    how = f" Identified from Shopify orders by {c.attribution} '{c.attribution_key}'." if c.attribution else ""
    return (
        f"Marketing campaign: {c.name} on {c.platform}, status {c.status}. Budget {_money(c.budget, currency)}, "
        f"spend {_money(c.spend, currency)}, impressions {c.impressions}, clicks {c.clicks}, "
        f"conversions (orders) {c.conversions}, attributed revenue {_money(c.revenue, currency)}, ROAS {roas}.{how}"
    )


def expense_text(e: Expense, currency: str) -> str:
    ref = f" (order {e.order_number})" if e.order_number else ""
    return f"Expense on {e.expense_date.isoformat()}: {e.category} - {e.description}{ref}: {_money(e.amount, currency)}."


def task_text(t: OpsTask) -> str:
    due = t.due_date.isoformat() if t.due_date else "no due date"
    return f"{t.domain.title()} task: {t.title}. Priority {t.priority}, status {t.status.replace('_', ' ')}, due {due}."


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _to_db_vector(vec: list[float]):
    return vec if settings.is_postgres else json.dumps(vec)


def _from_db_vector(value) -> list[float]:
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return [float(x) for x in value]


async def _write_chunks(db: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    vectors = await embeddings.embed([r["content"] for r in rows])
    model = embeddings.model_name()
    db.add_all(
        KnowledgeChunk(
            kind=r["kind"],
            ref_id=r.get("ref_id"),
            session_id=r.get("session_id"),
            content=r["content"],
            meta=r.get("meta"),
            embedding_model=model,
            embedding=_to_db_vector(v),
        )
        for r, v in zip(rows, vectors)
    )
    return len(rows)


async def rebuild_store_knowledge(db: AsyncSession, currency: str = "USD") -> int:
    """Replace every store-record chunk with fresh text from the database."""
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.kind.in_(STORE_KINDS)))

    rows: list[dict] = []
    for p in (await db.execute(select(Product))).scalars():
        rows.append({"kind": "product", "ref_id": p.sku, "content": product_text(p, currency), "meta": {"sku": p.sku}})

    lines_by_order: dict[int, list[OrderLine]] = {}
    for ln in (await db.execute(select(OrderLine))).scalars():
        lines_by_order.setdefault(ln.order_id, []).append(ln)
    for o in (await db.execute(select(Order))).scalars():
        rows.append(
            {
                "kind": "order",
                "ref_id": o.order_number,
                "content": order_text(o, lines_by_order.get(o.id, []), currency),
                "meta": {"order_number": o.order_number, "status": o.status},
            }
        )
    for c in (await db.execute(select(Campaign))).scalars():
        rows.append({"kind": "campaign", "ref_id": str(c.id), "content": campaign_text(c, currency), "meta": {"name": c.name}})
    for e in (await db.execute(select(Expense))).scalars():
        rows.append({"kind": "expense", "ref_id": str(e.id), "content": expense_text(e, currency), "meta": {"category": e.category}})
    for t in (await db.execute(select(OpsTask))).scalars():
        rows.append({"kind": "task", "ref_id": str(t.id), "content": task_text(t), "meta": {"priority": t.priority}})

    count = await _write_chunks(db, rows)
    await db.commit()
    logger.info("Indexed %d store records for retrieval", count)
    return count


async def remember_chat_turn(db: AsyncSession, session_id: str, question: str, answer: str) -> None:
    """Store one admin Q&A turn so later conversations can recall it."""
    content = f"Staff asked: {question.strip()}\nAgent answered: {answer.strip()}"
    if len(content) > MAX_CHAT_CHUNK_CHARS:
        content = content[: MAX_CHAT_CHUNK_CHARS - 1] + "…"
    await _write_chunks(
        db,
        [{"kind": CHAT_KIND, "session_id": session_id, "content": content, "meta": {"question": question[:200]}}],
    )
    await db.commit()


async def forget_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.session_id == session_id))
    await db.commit()


# --------------------------------------------------------------------------- #
# Searching
# --------------------------------------------------------------------------- #


async def search(
    db: AsyncSession,
    query: str,
    kinds: Iterable[str] | None = None,
    limit: int = 6,
    min_score: float = 0.0,
) -> list[Hit]:
    """Top-``limit`` chunks by cosine similarity to ``query``."""
    query = query.strip()
    if not query:
        return []
    vector = await embeddings.embed_one(query)
    model = embeddings.model_name()
    kind_list = list(kinds) if kinds else None

    if settings.is_postgres:
        distance = KnowledgeChunk.embedding.cosine_distance(vector).label("distance")
        stmt = select(KnowledgeChunk, distance).where(KnowledgeChunk.embedding_model == model)
        if kind_list:
            stmt = stmt.where(KnowledgeChunk.kind.in_(kind_list))
        stmt = stmt.order_by(distance).limit(limit)
        hits = [
            Hit(
                kind=row.kind,
                content=row.content,
                meta=row.meta or {},
                score=round(1.0 - float(dist), 4),
                session_id=row.session_id,
                created_at=row.created_at,
            )
            for row, dist in (await db.execute(stmt)).all()
        ]
    else:
        stmt = select(KnowledgeChunk).where(KnowledgeChunk.embedding_model == model)
        if kind_list:
            stmt = stmt.where(KnowledgeChunk.kind.in_(kind_list))
        scored: list[tuple[float, KnowledgeChunk]] = []
        for row in (await db.execute(stmt)).scalars():
            emb = _from_db_vector(row.embedding)
            if len(emb) != len(vector):
                continue
            scored.append((sum(a * b for a, b in zip(emb, vector)), row))
        scored.sort(key=lambda s: s[0], reverse=True)
        hits = [
            Hit(
                kind=row.kind,
                content=row.content,
                meta=row.meta or {},
                score=round(float(sim), 4),
                session_id=row.session_id,
                created_at=row.created_at,
            )
            for sim, row in scored[:limit]
        ]
    return [h for h in hits if h.score >= min_score]


async def build_context(db: AsyncSession, query: str, current_session: str | None = None) -> str:
    """Retrieved context to hand the admin agent alongside a question.

    Returns an empty string when nothing relevant is stored, so the caller can
    pass the question through untouched.
    """
    store_hits = await search(db, query, kinds=STORE_KINDS, limit=settings.RAG_STORE_RESULTS, min_score=0.05)
    memory_hits = await search(db, query, kinds=[CHAT_KIND], limit=settings.RAG_MEMORY_RESULTS + 2, min_score=0.1)
    # The current session's own turns are already in chat_history.
    memory_hits = [h for h in memory_hits if h.session_id != current_session][: settings.RAG_MEMORY_RESULTS]

    if not store_hits and not memory_hits:
        return ""
    parts = []
    if store_hits:
        parts.append("Store records matching the question:\n" + "\n".join(f"- {h.content}" for h in store_hits))
    if memory_hits:
        parts.append(
            "Relevant earlier conversations with staff:\n"
            + "\n".join(
                f"- ({h.created_at.strftime('%Y-%m-%d') if h.created_at else 'earlier'}) {h.content}" for h in memory_hits
            )
        )
    return "\n\n".join(parts)


async def stats(db: AsyncSession) -> dict:
    from sqlalchemy import func

    rows = (
        await db.execute(select(KnowledgeChunk.kind, func.count(KnowledgeChunk.id)).group_by(KnowledgeChunk.kind))
    ).all()
    return {
        "backend": "pgvector" if settings.is_postgres else "sqlite-json",
        "embedding_model": embeddings.model_name(),
        "chunks": {kind: count for kind, count in rows},
    }
