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
      lineItems(first: 20) {
        nodes {
          title
          quantity
          discountedTotalSet { shopMoney { amount currencyCode } }
          variant {
            legacyResourceId
            title
            media(first: 1) { nodes { ... on MediaImage { image { url } } } }
          }
          product {
            legacyResourceId
            handle
            productType
            tags
            onlineStoreUrl
            featuredMedia { ... on MediaImage { image { url } } }
          }
        }
      }
    }
  }
}
"""

CART_PRODUCTS = """
query SupportCartProducts($query: String!, $first: Int!) {
  products(first: $first, query: $query) {
    nodes {
      legacyResourceId
      title
      handle
      onlineStoreUrl
      featuredMedia { ... on MediaImage { image { url } } }
      variants(first: 100) {
        nodes {
          legacyResourceId
          title
          price
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""

ORDERS_BY_EMAIL = """
query SupportOrderHistory($query: String!, $first: Int!) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name
      createdAt
      cancelledAt
      displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) {
        nodes {
          title
          quantity
          discountedTotalSet { shopMoney { amount currencyCode } }
          variant {
            legacyResourceId
            title
            media(first: 1) { nodes { ... on MediaImage { image { url } } } }
          }
          product {
            legacyResourceId
            handle
            productType
            tags
            onlineStoreUrl
            featuredMedia { ... on MediaImage { image { url } } }
          }
        }
      }
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


def order_line_card(line: dict) -> dict:
    """One line of an order, with the picture and link for what was bought."""
    variant = line.get("variant") or {}
    product = line.get("product") or {}
    variant_id = str(variant["legacyResourceId"]) if variant.get("legacyResourceId") else None
    money = (line.get("discountedTotalSet") or {}).get("shopMoney") or {}
    quantity = line.get("quantity") or 1
    # Shopify gives the total for the line; the unit price is what a shopper reads.
    line_total = round(float(money["amount"]), 2) if money.get("amount") else None
    unit_price = round(line_total / quantity, 2) if line_total is not None and quantity else None
    option = variant.get("title")
    return {
        "product_id": str(product["legacyResourceId"]) if product.get("legacyResourceId") else None,
        "variant_id": variant_id,
        "title": line.get("title"),
        "option": None if option in (None, "Default Title") else option,
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
        "currency": money.get("currencyCode"),
        "image": variant_image(variant) or product_image(product),
        "url": product_url(product, variant_id) if product.get("handle") else None,
        "category": product.get("productType") or None,
        "tags": product.get("tags") or [],
        "handle": product.get("handle"),
    }


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
        "items": [order_line_card(line) for line in node["lineItems"]["nodes"]],
        "tracking": tracking,
        "estimated_delivery": estimated,
        # Shopify's tokenised order page: the token IS the authentication, so it
        # is only ever returned on this branch, where the email already matched.
        "status_page_url": node.get("statusPageUrl"),
    }


async def customer_orders(email: str, limit: int = 5) -> dict:
    """Recent orders for one email address, newest first.

    The caller must have established that the shopper really is this person -
    ``email:`` matches on the address alone, so this would otherwise read any
    customer's history from a guessed address.
    """
    address = (email or "").strip()
    if not address or "@" not in address:
        return {"found": False, "reason": "email_required", "orders": []}

    data = await graphql(ORDERS_BY_EMAIL, {"query": f'email:"{address}"', "first": max(1, min(limit, 20))})
    orders = []
    for node in data["orders"]["nodes"]:
        status = _fulfillment_status(node)
        money = node["totalPriceSet"]["shopMoney"]
        orders.append(
            {
                "order_number": node["name"],
                "placed_on": (node.get("createdAt") or "")[:10] or None,
                "status": status,
                "status_meaning": STATUS_MEANING.get(status, ""),
                "total": round(float(money["amount"]), 2),
                "currency": money["currencyCode"],
                "items": [order_line_card(line) for line in node["lineItems"]["nodes"]],
            }
        )
    return {"found": bool(orders), "count": len(orders), "orders": orders}


async def cart_cards(lines: list[dict], currency: str | None = None) -> list[dict]:
    """Give the shopper's own cart lines a picture and a link.

    The widget sends handles and variant ids but no imagery, so look the products
    up and attach the image for the exact variant in the cart - the blue hairband
    rather than whichever one happens to be featured.
    """
    handles = [str(line.get("handle")).strip() for line in lines if line.get("handle")]
    products: dict[str, dict] = {}
    if handles:
        joined = " OR ".join(f"handle:{h}" for h in dict.fromkeys(handles))
        data = await graphql(CART_PRODUCTS, {"query": joined, "first": min(len(handles) + 5, 50)})
        products = {node["handle"]: node for node in data["products"]["nodes"]}

    cards = []
    for line in lines:
        handle = str(line.get("handle") or "")
        node = products.get(handle)
        variant_id = str(line.get("variant_id") or "") or None
        image = None
        if node:
            variant = next(
                (v for v in node["variants"]["nodes"] if str(v.get("legacyResourceId")) == variant_id),
                None,
            )
            image = (variant_image(variant) if variant else None) or product_image(node)
        cards.append(
            {
                "product_id": str(line.get("product_id")) if line.get("product_id") else (
                    node.get("legacyResourceId") if node else None
                ),
                "variant_id": variant_id,
                "title": line.get("title") or (node["title"] if node else None),
                "option": line.get("variant_title"),
                "quantity": line.get("quantity") or 1,
                "price": minor_to_major(line.get("line_price")),
                "currency": currency,
                "image": image,
                "url": product_url(node, variant_id) if node else None,
            }
        )
    return cards


def minor_to_major(value) -> float | None:
    """Shopify sends cart money in the currency's minor unit: 2635 is 26.35."""
    if value is None:
        return None
    try:
        return round(int(value) / 100, 2)
    except (TypeError, ValueError):
        return None


__all__ = ["ShopifyError", "cart_cards", "customer_orders", "minor_to_major", "order_line_card", "find_order", "product_image", "product_url",
           "search_products", "shop_info", "variant_image"]
