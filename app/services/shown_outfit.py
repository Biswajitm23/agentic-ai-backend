"""The look a support chat last showed, so "yes, add it" adds that look.

build_outfit is called afresh whenever the agent likes, and two calls can price
two different looks - the reply showed one while a later "yes" rebuilt another
and queued a dress the shopper had never seen. So the look the reply actually
presented (the one CardCollector settled on) is kept per chat, replaced each
time a new look is shown, and add_look_to_cart reads it back.
"""

from sqlalchemy import select

from app.db.models import ShownOutfit
from app.db.session import AsyncSessionLocal


def _keep(outfit: dict) -> dict:
    """Only what adding it needs, and what the agent needs to talk about it."""
    return {
        "items": [{k: i.get(k) for k in ("title", "variant_id", "option", "price", "quantity", "url")}
                  for i in outfit.get("items") or []],
        "cart_items": [{"variant_id": c.get("variant_id"), "quantity": c.get("quantity") or 1}
                       for c in outfit.get("cart_items") or [] if c.get("variant_id")],
        "total": outfit.get("total"),
        "budget": outfit.get("budget"),
        "currency": outfit.get("currency"),
    }


async def remember(session_id: str, outfit: dict) -> None:
    """Keep the look this reply showed, in place of the last one."""
    kept = _keep(outfit)
    if not kept["cart_items"]:
        return
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ShownOutfit).where(ShownOutfit.session_id == session_id))).scalar_one_or_none()
        if row is None:
            db.add(ShownOutfit(session_id=session_id, outfit=kept))
        else:
            row.outfit = kept
        await db.commit()


async def recall(session_id: str | None) -> dict | None:
    """The look this chat last showed, or None when it has shown none."""
    if not session_id:
        return None
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ShownOutfit).where(ShownOutfit.session_id == session_id))).scalar_one_or_none()
    return dict(row.outfit) if row and row.outfit else None
