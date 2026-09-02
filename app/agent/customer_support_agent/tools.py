"""Tools for the customer support agent.

These read through ``services.storefront``, which deliberately exposes only what
a shopper may see — no cost, margin, campaign or whole-store figures, and no way
to list orders in bulk.
"""

import json

from langchain_core.tools import tool

from app.agent.customer_support_agent.store_info import STORE_POLICIES
from app.db.session import AsyncSessionLocal
from app.services import storefront


@tool
async def search_products(query: str) -> str:
    """Search the store's catalogue by product name, category or SKU and get price and stock availability.

    Pass a short search term such as "pre-workout", "powder" or a SKU. Pass an empty
    string to list what the store sells. Returns at most 10 matching products.
    """
    async with AsyncSessionLocal() as db:
        return json.dumps(await storefront.search_products(db, query))


@tool
async def list_product_categories() -> str:
    """List the product categories this store sells, with how many products are in each."""
    async with AsyncSessionLocal() as db:
        return json.dumps(await storefront.list_categories(db))


@tool
async def check_order_status(order_number: str) -> str:
    """Look up ONE order by its order number and get its current status, total and date placed.

    Requires the shopper's order number (for example "#1027" or "1027"). Returns
    found=false when there is no such order; do not guess numbers.
    """
    async with AsyncSessionLocal() as db:
        return json.dumps(await storefront.find_order(db, order_number))


@tool
async def get_store_policies() -> str:
    """Get the store's shipping, delivery, returns, refund, payment and contact policies."""
    return json.dumps(STORE_POLICIES)


CUSTOMER_SUPPORT_TOOLS = [
    search_products,
    list_product_categories,
    check_order_status,
    get_store_policies,
]
