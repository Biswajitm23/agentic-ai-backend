import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.registry import AGENTS, DEFAULT_AGENT, get_agent
from app.db.models import ChatMessage
from app.db.session import get_db

router = APIRouter(tags=["chat"])

HISTORY_LIMIT = 20


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    agent: str = DEFAULT_AGENT


class ChatResponse(BaseModel):
    session_id: str
    agent: str
    reply: str


@router.get("/chat/agents")
async def list_agents() -> dict:
    """The agents this API can route a chat to."""
    return {
        "agents": [
            {"name": a.name, "label": a.label, "description": a.description}
            for a in sorted(AGENTS.values(), key=lambda a: a.name)
        ],
        "default": DEFAULT_AGENT,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, db: AsyncSession = Depends(get_db)) -> ChatResponse:
    try:
        agent = get_agent(req.agent)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{req.agent}'") from None

    session_id = req.session_id or uuid.uuid4().hex

    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars().all()
    history = [(m.role, m.content) for m in reversed(rows)]

    reply = await agent.run(req.message, history)

    db.add_all(
        [
            ChatMessage(session_id=session_id, role="user", content=req.message),
            ChatMessage(session_id=session_id, role="assistant", content=reply),
        ]
    )
    await db.commit()
    return ChatResponse(session_id=session_id, agent=req.agent, reply=reply)
