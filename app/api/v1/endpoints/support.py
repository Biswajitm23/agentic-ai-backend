"""Customer-facing support chat.

Streams the customer support agent's reply over Server-Sent Events so the
shopper sees words appear as the agent writes them, plus a marker whenever the
agent looks something up. The agent is pinned here — unlike ``/chat`` this
endpoint takes no agent name, so a public client can never point it at the
admin agent and read internal business data.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.customer_support_agent import CUSTOMER_SUPPORT_AGENT
from app.agent.customer_support_agent.shopper_context import (
    Cart,
    Customer,
    PageContext,
    describe,
    with_context,
)
from app.api.v1.cards import CardCollector
from app.services import shopify_storefront, shopper_identity as identity
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["support"])

# Every stored turn is replayed on every step of the next turn, so this stays
# small: a shopper's thread rarely needs more than the last few exchanges.
HISTORY_LIMIT = 8
# Support conversations share the chat_messages table with the admin chat, so
# they carry their own session-id prefix and only ever load their own history.
SESSION_PREFIX = "cs_"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # stop nginx buffering the stream
}



class SupportChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    # Optional state from the storefront widget: what is in the cart, who is
    # signed in, and which page they are on. All of it is a claim from the
    # browser - see shopper_context and identity for what it may and may not do.
    cart: Cart | None = None
    customer: Customer | None = None
    context: PageContext | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_session(session_id: str | None) -> str:
    """Reuse a support session, or start one. Ids from elsewhere are not accepted."""
    if session_id and session_id.startswith(SESSION_PREFIX):
        return session_id
    return SESSION_PREFIX + uuid.uuid4().hex


async def _load_history(session_id: str) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_LIMIT)
            )
        ).scalars().all()
    return [(m.role, m.content) for m in reversed(rows)]


async def _save_turn(session_id: str, message: str, reply: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                ChatMessage(session_id=session_id, role="user", content=message),
                ChatMessage(session_id=session_id, role="assistant", content=reply),
            ]
        )
        await db.commit()


@router.post("/support/chat")
async def support_chat(req: SupportChatRequest) -> StreamingResponse:
    """Chat with the customer support agent. Replies stream back as SSE.

    Events, each with a JSON payload:
      session - {"session_id", "agent"}          first, so the client can keep the thread
      token   - {"text"}                         a piece of the reply
      reset   - {}                               clear the reply shown so far; the agent
                                                 was thinking out loud before a lookup
      tool    - {"name", "phase"}                the agent is looking something up
      cart    - {items[], currency, total,       the shopper's own cart, with an image
                 item_count}                      and link added to every line
      products- {items[], currency}               product cards to render: each has
                                                 product_id, variant_id, title, option,
                                                 price, image and url
      outfit  - {items[], currency, total,        a complete look: the same cards plus
                 budget, within_budget,           the exact total and the variants to
                 cart_items[]}                    add to the bag
      done    - {"session_id", "reply",          the finished reply, repeating
                 products?, outfit?}             whatever cards were produced
      error   - {"message"}                      the turn failed; nothing was saved
    """
    session_id = _resolve_session(req.session_id)
    history = await _load_history(session_id)
    # The briefing rides along with this turn only; history keeps the raw message.
    briefing = describe(req.cart, req.customer, req.context)
    shopper = identity.resolve(req.customer)

    async def events() -> AsyncIterator[str]:
        yield _sse("session", {"session_id": session_id, "agent": CUSTOMER_SUPPORT_AGENT.name})

        # The widget sends the cart without imagery, so hand it straight back with
        # pictures and links. Sent before the reply so the panel can draw at once.
        cart_payload: dict | None = None
        if req.cart and req.cart.items:
            try:
                cart_payload = {
                    "items": await shopify_storefront.cart_cards(
                        [line.model_dump() for line in req.cart.items], req.cart.currency
                    ),
                    "currency": req.cart.currency,
                    "item_count": req.cart.item_count,
                    "total": shopify_storefront.minor_to_major(req.cart.total_price),
                }
                yield _sse("cart", cart_payload)
            except Exception:
                logger.warning("Could not decorate the cart for session %s", session_id, exc_info=True)

        reply = ""
        cards = CardCollector()
        token = identity.set_current(shopper)
        try:
            async for event in CUSTOMER_SUPPORT_AGENT.stream(with_context(req.message, briefing), history):
                if event["type"] == "token":
                    yield _sse("token", {"text": event["text"]})
                elif event["type"] == "reset":
                    yield _sse("reset", {})
                elif event["type"] == "tool":
                    yield _sse("tool", {"name": event["name"], "phase": event["phase"]})
                    if event["phase"] == "end":
                        found = cards.take(event["name"], event.get("output"))
                        if found:
                            yield _sse(found[0], found[1])
                elif event["type"] == "final":
                    reply = event["reply"]
        except Exception:
            logger.exception("Support chat failed for session %s", session_id)
            yield _sse("error", {"message": "Sorry — something went wrong. Please try again."})
            return
        finally:
            identity.reset(token)

        await _save_turn(session_id, req.message, reply)
        # Repeated in `done` so a client that only reads the final event still
        # gets the cards without having to follow the stream.
        done_payload = {"session_id": session_id, "reply": reply, **cards.as_dict()}
        if cart_payload:
            done_payload["cart"] = cart_payload
        yield _sse("done", done_payload)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)
