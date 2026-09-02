"""Live, customer-safe reads from the Shopify Admin GraphQL API.

Every query here is fixed and parameterised — the agent chooses a search term or
an order number, never the shape of the request — and every response is
projected down to what a shopper may see:

* only ACTIVE products, because drafts and archived items cannot be bought;
* no unit cost, margin, inventory valuation or campaign figures;
* an order is reachable only by its number **and** the email address on it, and
  a wrong email is answered exactly like a number that does not exist, so the
  tool cannot be used to discover which orders are real.
"""

import logging

from app.services.shopify_client import ShopifyError, graphql, store_domain

logger = logging.getLogger(__name__)

PRODUCT_LIMIT = 10
VARIANT_LIMIT = 10

STATUS_MEANING = {
    "UNFULFILLED": "We have your order and it is queued for packing.",
    "IN_PROGRESS": "Your order is being packed and will ship shortly.",
    "PARTIALLY_FULFILLED": "Part of your order has shipped; the rest is on its way.",
    "FULFILLED": "Your order has shipped.",
    "ON_HOLD": "Your order is on hold. Our team will be in touch.",
    "SCHEDULED": "Your order is scheduled to ship.",
    "CANCELLED": "This order was cancelled.",
    "RESTOCKED": "This order was returned to stock.",
}

PRODUCT_SEARCH = """
query SupportProductSearch($query: String!, $first: Int!, $variants: Int!) {
  products(first: $first, query: $query, sortKey: RELEVANCE) {
    nodes {
      legacyResourceId
      title
      handle
      productType
      onlineStoreUrl
      totalInventory
      featuredMedia { ... on MediaImage { image { url altText } } }
      variants(first: $variants) {
        nodes {
          legacyResourceId
          sku
          title
          price
          compareAtPrice
          availableForSale
          inventoryQuantity
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""

ORDER_BY_NAME = """
query SupportOrderStatus($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      name
      email
      statusPageUrl
      createdAt
      cancelledAt
      displayFulfillmentStatus
      displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      fulfillments(first: 5) {
        status
        createdAt
        estimatedDeliveryAt
        deliveredAt
        trackingInfo { company number url }
      }
      lineItems(first: 20) { nodes { title quantity } }
    }
  }
}
"""

SHOP_INFO = """
query SupportShopInfo {
  shop { name currencyCode contactEmail url }
}
"""

_shop_cache: dict | None = None


async def shop_info() -> dict:
    """Store name, currency and contact address. Cached — it does not change."""
    global _shop_cache
    if _shop_cache is None:
        shop = (await graphql(SHOP_INFO))["shop"]
        _shop_cache = {
            "name": shop["name"],
            "currency": shop["currencyCode"],
            "contact_email": shop.get("contactEmail"),
            "url": shop.get("url"),
        }
    return _shop_cache


def product_image(node: dict) -> str | None:
    """The product's featured image, if it has one."""
    media = node.get("featuredMedia") or {}
    return (media.get("image") or {}).get("url")


def variant_image(variant: dict) -> str | None:
    """A variant's own photo - the pink shoe rather than the navy one."""
    nodes = (variant.get("media") or {}).get("nodes") or []
    for item in nodes:
        url = (item.get("image") or {}).get("url")
        if url:
            return url
    return None


def product_url(node: dict, variant_id: str | None = None) -> str:
    """A storefront link. onlineStoreUrl is null while the store is password
    protected, so fall back to the canonical /products/<handle> path, and point
    at the exact variant when one was chosen."""
    url = node.get("onlineStoreUrl") or f"https://{store_domain()}/products/{node['handle']}"
    return f"{url}?variant={variant_id}" if variant_id else url


def _availability(variants: list[dict]) -> str:
    if any(v["availableForSale"] for v in variants):
        return "in_stock"
    return "out_of_stock"


def _public_variant(v: dict, node: dict) -> dict:
    quantity = v.get("inventoryQuantity") or 0
    variant_id = v.get("legacyResourceId")
    return {
        "variant_id": variant_id,
        "sku": v.get("sku") or None,
        "option": None if v["title"] == "Default Title" else v["title"],
        "price": v["price"],
        "was_price": v.get("compareAtPrice"),
        "available": bool(v["availableForSale"]),
        "units_available": max(quantity, 0),
        "image": variant_image(v) or product_image(node),
        "url": product_url(node, variant_id),
    }


def _public_product(node: dict, currency: str) -> dict:
    variants = node["variants"]["nodes"]
    prices = [float(v["price"]) for v in variants if v.get("price") is not None]
    return {
        "product_id": node.get("legacyResourceId"),
        "title": node["title"],
        "category": node.get("productType") or None,
        "url": product_url(node),
        "image": product_image(node),
        "currency": currency,
        "price_from": round(min(prices), 2) if prices else None,
        "price_to": round(max(prices), 2) if prices else None,
        "availability": _availability(variants),
        "variants": [_public_variant(v, node) for v in variants],
    }


async def search_products(query: str = "", limit: int = PRODUCT_LIMIT) -> dict:
    """Search the live catalogue. Only ACTIVE products — a shopper cannot buy a draft."""
    term = " ".join(query.split()).strip()
    # The agent supplies only the term; the status filter is ours and always applied.
    search = f"({term}) AND status:ACTIVE" if term else "status:ACTIVE"
    currency = (await shop_info())["currency"]
    data = await graphql(
        PRODUCT_SEARCH,
        {"query": search, "first": max(1, min(limit, PRODUCT_LIMIT)), "variants": VARIANT_LIMIT},
    )
    products = [_public_product(n, currency) for n in data["products"]["nodes"]]
    return {"query": term, "currency": currency, "count": len(products), "products": products}


def order_number_variants(raw: str) -> list[str]:
    """Accept '#1027' or '1027' for the same order."""
    given = raw.strip()
    bare = given.lstrip("#").strip()
    return list(dict.fromkeys([given, f"#{bare}", bare]))


def _fulfillment_status(node: dict) -> str:
    if node.get("cancelledAt"):
        return "CANCELLED"
    return node.get("displayFulfillmentStatus") or "UNFULFILLED"


async def find_order(order_number: str, email: str) -> dict:
    """Look one order up by number, released only when the email on it matches.

    A wrong email returns the same ``found: False`` as an order that does not
    exist, so this cannot be used to probe which order numbers are real. Only on
    a match does the response carry ``status_page_url`` - Shopify's tokenised
    order page, which anyone holding the link can open.
    """
    given_email = email.strip().casefold()
    if not given_email or "@" not in given_email:
        return {"found": False, "reason": "email_required"}

    node = None
    for candidate in order_number_variants(order_number):
        data = await graphql(ORDER_BY_NAME, {"query": f"name:{candidate}"})
        nodes = data["orders"]["nodes"]
        if nodes:
            node = nodes[0]
            break

    if node is None or (node.get("email") or "").strip().casefold() != given_email:
        return {"found": False, "reason": "no_match"}

    status = _fulfillment_status(node)
    money = node["totalPriceSet"]["shopMoney"]
    tracking = [
        {"carrier": t.get("company"), "number": t.get("number"), "url": t.get("url")}
        for f in node.get("fulfillments") or []
        for t in f.get("trackingInfo") or []
    ]
    estimated = next(
        (f.get("estimatedDeliveryAt") for f in node.get("fulfillments") or [] if f.get("estimatedDeliveryAt")),
        None,
    )
    return {
        "found": True,
        "order_number": node["name"],
        "placed_on": (node.get("createdAt") or "")[:10] or None,
        "status": status,
        "status_meaning": STATUS_MEANING.get(status, "Please contact support for the current status."),
        "payment_status": node.get("displayFinancialStatus"),
        "total": round(float(money["amount"]), 2),
        "currency": money["currencyCode"],
        "items": [
            {"title": i["title"], "quantity": i["quantity"]}
            for i in node["lineItems"]["nodes"]
        ],
        "tracking": tracking,
        "estimated_delivery": estimated,
        # Shopify's tokenised order page: the token IS the authentication, so it
        # is only ever returned on this branch, where the email already matched.
        "status_page_url": node.get("statusPageUrl"),
    }


__all__ = ["ShopifyError", "find_order", "product_image", "product_url",
           "search_products", "shop_info", "variant_image"]
