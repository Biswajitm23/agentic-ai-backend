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
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["support"])

HISTORY_LIMIT = 20
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
      done    - {"session_id", "reply"}          the finished reply
      error   - {"message"}                      the turn failed; nothing was saved
    """
    session_id = _resolve_session(req.session_id)
    history = await _load_history(session_id)

    async def events() -> AsyncIterator[str]:
        yield _sse("session", {"session_id": session_id, "agent": CUSTOMER_SUPPORT_AGENT.name})
        reply = ""
        try:
            async for event in CUSTOMER_SUPPORT_AGENT.stream(req.message, history):
                if event["type"] == "token":
                    yield _sse("token", {"text": event["text"]})
                elif event["type"] == "reset":
                    yield _sse("reset", {})
                elif event["type"] == "tool":
                    yield _sse("tool", {"name": event["name"], "phase": event["phase"]})
                elif event["type"] == "final":
                    reply = event["reply"]
        except Exception:
            logger.exception("Support chat failed for session %s", session_id)
            yield _sse("error", {"message": "Sorry — something went wrong. Please try again."})
            return

        await _save_turn(session_id, req.message, reply)
        yield _sse("done", {"session_id": session_id, "reply": reply})

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)
