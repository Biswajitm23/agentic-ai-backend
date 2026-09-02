"""The admin agent: inventory, marketing, operations and finance for this store."""

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


@lru_cache(maxsize=1)
def get_admin_agent_executor() -> AgentExecutor:
    return build_agent_executor(ADMIN_SYSTEM_PROMPT, ADMIN_TOOLS)


async def run_admin_agent(message: str, history: ChatHistory) -> str:
    """Run one admin-agent turn. history is a list of (role, content) with role 'user' or 'assistant'."""
    return await run_executor(get_admin_agent_executor(), message, history)


def stream_admin_agent(message: str, history: ChatHistory) -> AsyncIterator[AgentEvent]:
    """Run one admin-agent turn, yielding reply tokens and tool activity as they happen."""
    return stream_executor(get_admin_agent_executor(), message, history)


ADMIN_AGENT = Agent(
    name="admin",
    label="Business Intelligence",
    description="Answers staff questions about inventory, marketing, operations and finance.",
    run=run_admin_agent,
    stream=stream_admin_agent,
)
