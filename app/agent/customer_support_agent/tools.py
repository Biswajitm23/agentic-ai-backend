"""Tools for the customer support agent — live reads from the Shopify store.

Each tool wraps one fixed GraphQL operation in ``services.shopify_storefront``.
The agent supplies a search term, an order number or an email; it never composes
a query, so a shopper cannot steer what is asked of the Admin API. Failures come
back as a plain message the agent can relay instead of raising, so a Shopify
outage degrades into an apology rather than a broken conversation.
"""

import json
import logging

from langchain_core.tools import tool

from app.agent.customer_support_agent.store_info import STORE_POLICIES
from app.services import shopify_storefront
from app.services.shopify_client import ShopifyError

logger = logging.getLogger(__name__)

UNAVAILABLE = {
    "error": "The store systems could not be reached just now.",
    "tell_customer": "I can't reach our store systems at the moment - please try again in a minute.",
}


def _fail(where: str, exc: Exception) -> str:
    logger.warning("Shopify tool %s failed: %s", where, exc)
    return json.dumps(UNAVAILABLE)


@tool
async def search_products(query: str) -> str:
    """Search the store's live catalogue for products a shopper can actually buy.

    Pass a short search term such as "hairband", "shoes" or a product name. Pass an
    empty string to see what the store sells. Returns title, price range, currency,
    stock availability and the product page URL for up to 10 products. Only products
    that are live on the storefront are returned.
    """
    try:
        return json.dumps(await shopify_storefront.search_products(query), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("search_products", exc)


@tool
async def check_order_status(order_number: str, email: str) -> str:
    """Look up ONE order and get its status, items, total and tracking.

    Needs BOTH the order number (for example "#1027" or "1027") and the email
    address the order was placed with - the order is only released when the email
    matches. If either is missing, ask the shopper for it; never guess an email.
    Returns found=false when the number and email do not match an order.
    """
    try:
        return json.dumps(
            await shopify_storefront.find_order(order_number, email), ensure_ascii=False
        )
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("check_order_status", exc)


@tool
async def get_store_info() -> str:
    """Get the store's name, the currency prices are shown in, and its contact email."""
    try:
        return json.dumps(await shopify_storefront.shop_info(), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_store_info", exc)


@tool
async def get_store_policies() -> str:
    """Get the store's shipping, delivery, returns, refund, payment and contact policies."""
    return json.dumps(STORE_POLICIES, ensure_ascii=False)


CUSTOMER_SUPPORT_TOOLS = [
    search_products,
    check_order_status,
    get_store_info,
    get_store_policies,
]
