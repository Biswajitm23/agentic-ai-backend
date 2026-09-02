"""Tools for the admin agent: read-only views over the store's own analytics."""

import json

from langchain_core.tools import tool

from app.db.session import AsyncSessionLocal
from app.services import insights


@tool
async def get_inventory_data() -> str:
    """Get the store's current inventory data: SKUs, stock levels, stock value, low-stock alerts and per-product details."""
    async with AsyncSessionLocal() as db:
        return json.dumps(await insights.inventory_summary(db))


@tool
async def get_marketing_data() -> str:
    """Get the store's marketing data: campaigns with platform, status, budget, spend, impressions, clicks, conversions, revenue, ROAS and CTR."""
    async with AsyncSessionLocal() as db:
        return json.dumps(await insights.marketing_summary(db))


@tool
async def get_operations_data() -> str:
    """Get the store's operations data: order counts by fulfillment status, recent orders, and operational tasks with priority and due dates."""
    async with AsyncSessionLocal() as db:
        return json.dumps(await insights.operations_summary(db))


@tool
async def get_finance_data() -> str:
    """Get the store's finance data: total revenue, expenses by category, net profit and profit margin."""
    async with AsyncSessionLocal() as db:
        return json.dumps(await insights.finance_summary(db))


ADMIN_TOOLS = [get_inventory_data, get_marketing_data, get_operations_data, get_finance_data]
