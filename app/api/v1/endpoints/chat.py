"""Staff chat with a named agent (the admin agent by default).

Replies can be fetched whole (``POST /chat``) or streamed over Server-Sent Events
(``POST /chat/stream``) so the dashboard can show the reply as it is written and
which tool the agent is consulting.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.registry import AGENTS, DEFAULT_AGENT, get_agent
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal, get_db
from app.services import rag

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

HISTORY_LIMIT = 20

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    agent: str = DEFAULT_AGENT


class ChatResponse(BaseModel):
    session_id: str
    agent: str
    reply: str


class HistoryMessage(BaseModel):
    role: str
    content: str
    created_at: str | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _load_history(db: AsyncSession, session_id: str) -> list[tuple[str, str]]:
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars().all()
    return [(m.role, m.content) for m in reversed(rows)]


async def _save_turn(db: AsyncSession, session_id: str, message: str, reply: str) -> None:
    db.add_all(
        [
            ChatMessage(session_id=session_id, role="user", content=message),
            ChatMessage(session_id=session_id, role="assistant", content=reply),
        ]
    )
    await db.commit()


def _resolve_agent(name: str):
    try:
        return get_agent(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{name}'") from None


@router.get("/chat/agents")
async def list_agents() -> dict:
    """The agents this API can route a chat to."""
    return {
        "agents": [
            {"name": a.name, "label": a.label, "description": a.description, "has_memory": a.remember is not None}
            for a in sorted(AGENTS.values(), key=lambda a: a.name)
        ],
        "default": DEFAULT_AGENT,
    }


@router.get("/chat/memory")
async def memory_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """What the retrieval index holds: backend (pgvector or the SQLite fallback), model, chunk counts."""
    return await rag.stats(db)


@router.get("/chat/{session_id}/history", response_model=list[HistoryMessage])
async def chat_history(session_id: str, db: AsyncSession = Depends(get_db)) -> list[HistoryMessage]:
    rows = (
        await db.execute(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id))
    ).scalars().all()
    return [
        HistoryMessage(role=m.role, content=m.content, created_at=m.created_at.isoformat() if m.created_at else None)
        for m in rows
    ]


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, db: AsyncSession = Depends(get_db)) -> ChatResponse:
    agent = _resolve_agent(req.agent)
    session_id = req.session_id or uuid.uuid4().hex
    history = await _load_history(db, session_id)

    reply = await agent.run(req.message, history)

    await _save_turn(db, session_id, req.message, reply)
    if agent.remember is not None:
        await agent.remember(session_id, req.message, reply)
    return ChatResponse(session_id=session_id, agent=req.agent, reply=reply)


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same as ``/chat`` but the reply streams back as SSE.

    Events, each with a JSON payload:
      session - {"session_id", "agent"}   first, so the client can keep the thread
      token   - {"text"}                  a piece of the reply
      reset   - {}                        clear the reply shown so far (the agent was
                                          thinking out loud before a tool call)
      tool    - {"name", "phase"}         the agent is consulting a tool
      done    - {"session_id", "reply"}   the finished reply
      error   - {"message"}               the turn failed; nothing was saved
    """
    agent = _resolve_agent(req.agent)
    session_id = req.session_id or uuid.uuid4().hex
    async with AsyncSessionLocal() as db:
        history = await _load_history(db, session_id)

    async def events() -> AsyncIterator[str]:
        yield _sse("session", {"session_id": session_id, "agent": agent.name})
        reply = ""
        try:
            async for event in agent.stream(req.message, history):
                if event["type"] == "token":
                    yield _sse("token", {"text": event["text"]})
                elif event["type"] == "reset":
                    yield _sse("reset", {})
                elif event["type"] == "tool":
                    yield _sse("tool", {"name": event["name"], "phase": event["phase"]})
                elif event["type"] == "final":
                    reply = event["reply"]
        except Exception:
            logger.exception("Chat stream failed for session %s", session_id)
            yield _sse("error", {"message": "The agent hit an error. Please try again."})
            return

        async with AsyncSessionLocal() as db:
            await _save_turn(db, session_id, req.message, reply)
        if agent.remember is not None:
            await agent.remember(session_id, req.message, reply)
        yield _sse("done", {"session_id": session_id, "reply": reply})

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)
