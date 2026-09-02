"""Tools for the customer support agent.

Live store data comes from fixed GraphQL operations in ``shopify_storefront`` and
``outfit``; how the store itself works - paths, collections, policies - comes from
the handbook index in ``handbook``. The agent supplies a search term, an order
number, an email or a question; it never composes a query, so a shopper cannot
steer what is asked of the Admin API. Failures come back as a plain message the
agent can relay instead of raising, so an outage degrades into an apology rather
than a broken conversation.
"""

import json
import logging

from langchain_core.tools import tool

from app.db.session import AsyncSessionLocal
from app.services import handbook, outfit, shopify_storefront
from app.services import shopper_identity as identity
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
    """Search the live catalogue for one thing a shopper named.

    query: a short term like "hairband" or a product name; empty lists what is sold.
    Returns up to 10 buyable products with price, currency and stock.
    """
    try:
        return json.dumps(await shopify_storefront.search_products(query), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("search_products", exc)


@tool
async def check_order_status(order_number: str, email: str) -> str:
    """Look up ONE order: status, items, total, tracking.

    Needs BOTH the order number ("#1027" or "1027") and the email on the order;
    it is released only when they match. found=false means they did not.
    """
    try:
        return json.dumps(
            await shopify_storefront.find_order(order_number, email), ensure_ascii=False
        )
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("check_order_status", exc)


@tool
async def get_store_info() -> str:
    """Store name, currency and contact email."""
    try:
        return json.dumps(await shopify_storefront.shop_info(), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_store_info", exc)


@tool
async def get_store_policies() -> str:
    """Shipping, delivery, returns, refunds and contact details, from the store handbook.

    Read the result carefully: anything still marked with an empty box has not been
    decided yet, and must NOT be guessed at - offer a human instead.
    """
    try:
        async with AsyncSessionLocal() as db:
            found = await handbook.search(db, "refund return shipping delivery policy terms", limit=4)
        return json.dumps({"handbook": found}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - a retrieval failure must not break the chat
        return _fail("get_store_policies", exc)


@tool
async def search_store_handbook(question: str) -> str:
    """Look up how this store works: account pages, collections, cart and checkout paths, policies.

    Use for "where do I find...", "how do I return...", "do you have a size guide" - anything
    about the store itself rather than a product or an order. Passages come from the store's
    own handbook. Items marked with a warning sign are unconfirmed and items marked with an
    empty box are not filled in yet: never state either as fact, and never send a link you
    were not given verbatim.
    """
    try:
        async with AsyncSessionLocal() as db:
            return json.dumps({"passages": await handbook.search(db, question, limit=4)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return _fail("search_store_handbook", exc)



@tool
async def browse_catalogue() -> str:
    """Everything buyable right now, by category - use before build_outfit.

    For an outfit, gift or occasion rather than one named product. Returns each
    product's handle, category, price, colours, sizes, and the store currency.
    """
    try:
        return json.dumps(await outfit.browse_catalogue(), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("browse_catalogue", exc)


@tool
async def build_outfit(items: str | list, budget: float = 0) -> str:
    """Price a look exactly and get its variant ids. Never add prices up yourself.

    items: JSON array using handles/colours/sizes from browse_catalogue, e.g.
      [{"handle": "gingham-dress", "color": "Pink", "size": "5Y", "quantity": 1}]
    Omit color/size where the product has none. budget: 0 if not given.
    Returns total, within_budget, cart_items (variant ids for the storefront), and
    problems listing the colours/sizes that do exist so you can swap and retry.
    """
    try:
        result = await outfit.build_outfit(items, budget or None)
        return json.dumps(result, ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("build_outfit", exc)



NOT_SIGNED_IN = {
    "signed_in": False,
    "tell_customer": (
        "I can only pull up your order history once I know it is you. Give me an order "
        "number and the email it was placed with and I can check that order directly."
    ),
}


@tool
async def get_my_order_history() -> str:
    """Past orders for the shopper this chat belongs to. Takes no arguments.

    Only works when the storefront has signed them in and this deployment trusts
    that; otherwise it returns signed_in=false and you should ask for an order
    number and email instead. You cannot look up anybody else with this.
    """
    shopper = identity.current()
    if shopper is None:
        return json.dumps(NOT_SIGNED_IN)
    try:
        return json.dumps(await shopify_storefront.customer_orders(shopper.email), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("get_my_order_history", exc)


@tool
async def recommend_for_me() -> str:
    """Suggest products for this shopper based on what they have bought before. No arguments.

    Use when a signed-in shopper asks what they might like, or for a gift for the
    same child. Returns products they do not already own, each with why it was
    picked. Falls back to signed_in=false when there is no verified shopper.
    """
    shopper = identity.current()
    if shopper is None:
        return json.dumps(NOT_SIGNED_IN)
    try:
        history = await shopify_storefront.customer_orders(shopper.email, limit=10)
        return json.dumps(await outfit.recommend_from_orders(history["orders"]), ensure_ascii=False)
    except (ShopifyError, KeyError, ValueError) as exc:
        return _fail("recommend_for_me", exc)


CUSTOMER_SUPPORT_TOOLS = [
    search_products,
    browse_catalogue,
    build_outfit,
    check_order_status,
    get_my_order_history,
    recommend_for_me,
    get_store_info,
    get_store_policies,
    search_store_handbook,
]
