"""The products a chat is still adding to the bag, one question at a time.

Every add_to_cart call in the chat picks these up again, so the agent only has
to pass what the shopper just answered: the rest stay in the queue until each
has its size and colour, or the shopper says they want only some.
"""

from sqlalchemy import delete, select

from app.db.models import PendingAdds
from app.db.session import AsyncSessionLocal


async def recall(session_id: str | None) -> list[dict]:
    """What this chat is still waiting to add, or []."""
    if not session_id:
        return []
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(PendingAdds).where(PendingAdds.session_id == session_id))).scalar_one_or_none()
    return list(row.items or []) if row else []


async def keep(session_id: str | None, items: list[dict]) -> None:
    """Replace the queue with these items; an empty list clears it."""
    if not session_id:
        return
    async with AsyncSessionLocal() as db:
        if not items:
            await db.execute(delete(PendingAdds).where(PendingAdds.session_id == session_id))
        else:
            row = (await db.execute(select(PendingAdds).where(PendingAdds.session_id == session_id))).scalar_one_or_none()
            if row is None:
                db.add(PendingAdds(session_id=session_id, items=items))
            else:
                row.items = items
        await db.commit()
