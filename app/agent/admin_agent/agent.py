"""The admin agent: inventory, marketing, operations and finance for this store.

Every turn is retrieval-augmented: before the model sees the question, the
pgvector index (``services.rag``) is searched for store records and earlier staff
conversations that match it, and those are attached as context. After a turn is
answered, the question and reply are embedded and stored so later conversations -
in any session - can recall them.
"""

import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from langchain.agents import AgentExecutor

from app.agent.admin_agent.prompts import ADMIN_SYSTEM_PROMPT
from app.agent.admin_agent.tools import ADMIN_TOOLS
from app.agent.base import (
    Agent,
    AgentEvent,
    ChatHistory,
    build_agent_executor,
    run_executor,
    stream_executor,
)
from app.db.session import AsyncSessionLocal
from app.services import rag

logger = logging.getLogger(__name__)

CONTEXT_HEADER = "Retrieved context (from the store's records and earlier staff conversations; verify totals with tools):"


@lru_cache(maxsize=1)
def get_admin_agent_executor() -> AgentExecutor:
    return build_agent_executor(ADMIN_SYSTEM_PROMPT, ADMIN_TOOLS)


async def _augment(message: str, history: ChatHistory) -> str:
    """The question plus any retrieved context worth attaching."""
    try:
        async with AsyncSessionLocal() as db:
            context = await rag.build_context(db, message)
    except Exception:
        logger.exception("Retrieval failed; answering without RAG context")
        return message
    if not context:
        return message
    # Turns already in this conversation's history need not be repeated.
    asked = {content.strip() for role, content in history if role == "user"}
    lines = [ln for ln in context.splitlines() if not any(f"Staff asked: {q}" in ln for q in asked)]
    context = "\n".join(lines).strip()
    if not context or context.endswith(":"):
        return message
    return f"{message}\n\n---\n{CONTEXT_HEADER}\n{context}"


async def run_admin_agent(message: str, history: ChatHistory) -> str:
    """Run one admin-agent turn. history is a list of (role, content) with role 'user' or 'assistant'."""
    return await run_executor(get_admin_agent_executor(), await _augment(message, history), history)


async def stream_admin_agent(message: str, history: ChatHistory) -> AsyncIterator[AgentEvent]:
    """Run one admin-agent turn, yielding reply tokens and tool activity as they happen."""
    augmented = await _augment(message, history)
    async for event in stream_executor(get_admin_agent_executor(), augmented, history):
        yield event


async def remember_admin_turn(session_id: str, message: str, reply: str) -> None:
    """Embed and store a finished turn for cross-session recall."""
    try:
        async with AsyncSessionLocal() as db:
            await rag.remember_chat_turn(db, session_id, message, reply)
    except Exception:
        logger.exception("Could not store the admin chat turn in the retrieval index")


ADMIN_AGENT = Agent(
    name="admin",
    label="Business Intelligence",
    description="Answers staff questions about inventory, marketing, operations and finance, with memory of earlier conversations.",
    run=run_admin_agent,
    stream=stream_admin_agent,
    remember=remember_admin_turn,
)
