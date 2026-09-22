"""What a support chat last showed the shopper, so "checkout" can mean it.

A shopper who asks for a brown belt, is shown one, and then says "checkout" with
an empty bag wants that belt. The reply named it, but the transcript is only
words; this keeps the products themselves, one set per chat, replaced every
time a reply shows products.
"""

import logging

from sqlalchemy import select

from app.db.models import ShownProducts
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

MAX_REMEMBERED = 12


async def remember(session_id: str, products: list[dict]) -> None:
    """Keep the products this reply showed, in place of the last ones."""
    kept, seen = [], set()
    for product in products:
        key = product.get("product_id") or product.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append({"product_id": product.get("product_id"), "variant_id": product.get("variant_id"),
                     "title": product.get("title")})
        if len(kept) >= MAX_REMEMBERED:
            break
    if not kept:
        return
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ShownProducts).where(ShownProducts.session_id == session_id))).scalar_one_or_none()
        if row is None:
            db.add(ShownProducts(session_id=session_id, products=kept))
        else:
            row.products = kept
        await db.commit()


async def recall(session_id: str) -> list[dict]:
    """The products this chat last showed, or [] when it has shown none."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ShownProducts).where(ShownProducts.session_id == session_id))).scalar_one_or_none()
    return list(row.products or []) if row else []
