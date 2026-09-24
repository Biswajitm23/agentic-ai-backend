"""Shopify's own product categories - the "Category" field on each product.

Not the store's collections, and not the free-text product type: this is the
Standard Product Taxonomy entry the merchant picks in admin ("Baby & Children's
One-Pieces"). Each carries a real id, gid://shopify/TaxonomyCategory/aa-1-25-3,
and the part after the last "/" is what the products search takes as
``category_id``.

Like every other read, the queries are fixed and only ACTIVE products count.
"""

import time

from app.core.config import settings
from app.services.shopify_client import graphql
from app.services.shopify_storefront import _fresh, product_url, shop_info

STORE_CATEGORIES = """
query StoreCategories($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      category { id name fullName }
      featuredMedia { preview { image { url } } }
    }
  }
}
"""

PRODUCTS_IN_CATEGORY = """
query ProductsInCategory($q: String!) {
  products(first: 50, query: $q) {
    nodes {
      legacyResourceId
      title
      handle
      onlineStoreUrl
      featuredMedia { preview { image { url } } }
      priceRangeV2 { minVariantPrice { amount currencyCode } }
    }
  }
}
"""

CATEGORY_PRODUCT_LIMIT = 50    # what one ProductsInCategory call returns

# The whole list, cached as one: every caller wants all of it.
_cache: tuple[float, list[dict]] | None = None


def _image(node: dict) -> str | None:
    media = node.get("featuredMedia") or {}
    return ((media.get("preview") or {}).get("image") or {}).get("url")


def short_id(gid: str) -> str:
    """gid://shopify/TaxonomyCategory/aa-1-25-3 -> aa-1-25-3."""
    return gid.rsplit("/", 1)[-1]


async def store_categories() -> list[dict]:
    """Every category an active product sits in, biggest first. Cached."""
    global _cache
    if _fresh(_cache, settings.SUPPORT_PRODUCT_CATEGORY_CACHE_MINUTES):
        return _cache[1]

    groups: dict[str, dict] = {}
    cursor: str | None = None
    while True:
        page = (await graphql(STORE_CATEGORIES, {"cursor": cursor}))["products"]
        for node in page["nodes"]:
            category = node.get("category")
            if not category:
                continue
            group = groups.setdefault(category["id"], {
                "id": short_id(category["id"]),
                "name": category["name"],
                "full_name": category.get("fullName") or category["name"],
                "product_count": 0,
                "image": None,
            })
            group["product_count"] += 1
            group["image"] = group["image"] or _image(node)
        info = page["pageInfo"]
        if not info["hasNextPage"] or not info.get("endCursor"):
            break
        cursor = info["endCursor"]

    # Name breaks a tie, so the order does not shuffle between calls.
    ranked = sorted(groups.values(), key=lambda c: (-c["product_count"], c["name"].casefold()))
    _cache = (time.monotonic(), ranked)
    return ranked


def _find(reference: str, listed: list[dict]) -> dict | None:
    """The category with exactly this name or id, ignoring case. The agent picks
    the names from the list, so there is no guessing here."""
    key = " ".join((reference or "").split()).casefold().replace("’", "'")
    return next((c for c in listed
                 if key in (c["id"].casefold(), c["name"].casefold().replace("’", "'"),
                            c["full_name"].casefold().replace("’", "'"))), None)


async def products_in(references: list[str]) -> dict:
    """Every active product in the categories the agent chose."""
    listed = await store_categories()
    chosen, missing = [], []
    for ref in references:
        found = _find(ref, listed)
        if found is None:
            missing.append(ref)
        elif found not in chosen:
            chosen.append(found)
    if missing or not chosen:
        return {
            "found": False,
            "not_a_category": missing,
            "categories": [{k: c[k] for k in ("id", "name", "full_name", "product_count")} for c in listed],
        }
    heading = " & ".join(c["name"] for c in chosen)

    ids = [f'category_id:{c["id"]}' for c in chosen]
    # category_id is the exact category only, never what sits beneath it, so
    # several are ORed together.
    q = f"status:active AND {ids[0]}" if len(ids) == 1 else f'status:active AND ({" OR ".join(ids)})'
    nodes = (await graphql(PRODUCTS_IN_CATEGORY, {"q": q}))["products"]["nodes"]

    fallback_currency = (await shop_info())["currency"]
    products = []
    for node in nodes:
        price = ((node.get("priceRangeV2") or {}).get("minVariantPrice") or {})
        products.append({
            "product_id": node.get("legacyResourceId"),
            "title": node["title"],
            "handle": node["handle"],
            "url": product_url(node),
            "image": _image(node),
            "price_from": round(float(price["amount"]), 2) if price.get("amount") else None,
            "currency": price.get("currencyCode") or fallback_currency,
        })
    return {
        "found": True,
        "category": heading,
        "category_ids": [c["id"] for c in chosen],
        "heading": heading,
        "currency": products[0]["currency"] if products else fallback_currency,
        "count": len(products),
        "more_available": len(nodes) == CATEGORY_PRODUCT_LIMIT,
        "products": products,
    }
