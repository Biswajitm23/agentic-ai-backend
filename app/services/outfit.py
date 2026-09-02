"""Outfit building for the customer support agent.

The agent decides *what* goes together - that is a judgement about occasion, age
and style. This module owns everything the agent must not be trusted to do
itself: resolving a choice to a real purchasable variant, checking it is in
stock, adding the money up exactly, comparing it to the shopper's budget, and
naming the exact variant ids. Adding the look to the bag is the frontend's job -
it gets ``cart_items`` and takes it from there.

Reads are live from the Shopify Admin API and restricted to ACTIVE products, so
a look can never contain something a shopper cannot buy.
"""

import json
import logging
from decimal import Decimal, InvalidOperation

from app.services.shopify_client import ShopifyError, graphql
from app.services.shopify_storefront import (
    product_image,
    product_url,
    shop_info,
    variant_image,
)

logger = logging.getLogger(__name__)

MAX_PRODUCTS = 50
MAX_VARIANTS = 100
MAX_OUTFIT_ITEMS = 8

OUTFIT_FORMAT = (
    '[{"handle": "product-handle", "color": "Pink", "size": "5Y", "quantity": 1}] '
    "- color and size may be omitted for products that do not have them"
)

# productType is authoritative when the merchant sets it. Most of this store's
# products leave it blank, so fall back to reading the title.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Shoes", ("shoe", "boot", "sandal", "trainer", "sneaker", "pump", "loafer")),
    ("Dress", ("dress", "pinafore", "romper", "gown")),
    ("Top", ("shirt", "blouse", "top", "jumper", "cardigan", "sweater", "sweatshirt", "tee")),
    ("Bottoms", ("trouser", "short", "skirt", "legging", "jean", "dungaree")),
    ("Outerwear", ("coat", "jacket", "gilet")),
    ("Accessory", ("hairband", "headband", "bow", "hat", "cap", "sock", "tight",
                   "bag", "belt", "scarf", "clip", "bib")),
]

CATALOGUE = """
query OutfitCatalogue($query: String!, $first: Int!, $variants: Int!) {
  products(first: $first, query: $query, sortKey: TITLE) {
    nodes {
      legacyResourceId
      title
      handle
      productType
      onlineStoreUrl
      featuredMedia { ... on MediaImage { image { url altText } } }
      options { name values }
      variants(first: $variants) {
        nodes {
          legacyResourceId
          sku
          title
          price
          availableForSale
          inventoryQuantity
          selectedOptions { name value }
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""


def _category(title: str, product_type: str | None) -> str:
    if product_type:
        return product_type
    lowered = title.lower()
    for label, keywords in CATEGORY_RULES:
        if any(word in lowered for word in keywords):
            return label
    return "Other"


def _money(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _option_value(variant: dict, name: str) -> str | None:
    for option in variant.get("selectedOptions") or []:
        if option["name"].casefold() == name.casefold():
            return option["value"]
    return None


def _colour_of(variant: dict) -> str | None:
    return _option_value(variant, "Color") or _option_value(variant, "Colour")


def _options_of(product: dict) -> dict[str, list[str]]:
    return {o["name"]: o["values"] for o in product.get("options") or []}


async def _active_products(handles: list[str] | None = None) -> list[dict]:
    """Live ACTIVE products. A draft or archived product is never returned."""
    query = "status:ACTIVE"
    if handles:
        joined = " OR ".join(f"handle:{h}" for h in handles)
        query = f"({joined}) AND status:ACTIVE"
    data = await graphql(CATALOGUE, {"query": query, "first": MAX_PRODUCTS, "variants": MAX_VARIANTS})
    return data["products"]["nodes"]


async def browse_catalogue() -> dict:
    """Everything a shopper can buy, grouped by category so a look can be composed."""
    currency = (await shop_info())["currency"]
    products = []
    for node in await _active_products():
        variants = node["variants"]["nodes"]
        prices = [_money(v["price"]) for v in variants if v.get("price")]
        options = _options_of(node)
        products.append(
            {
                "handle": node["handle"],
                "product_id": node.get("legacyResourceId"),
                "title": node["title"],
                "category": _category(node["title"], node.get("productType")),
                "price_from": float(min(prices)) if prices else None,
                "price_to": float(max(prices)) if prices else None,
                "in_stock": any(v["availableForSale"] for v in variants),
                "colors": options.get("Color") or options.get("Colour") or [],
                "sizes": options.get("Size") or [],
                "image": product_image(node),
                "url": product_url(node),
            }
        )
    by_category: dict[str, list[str]] = {}
    for product in products:
        by_category.setdefault(product["category"], []).append(product["handle"])
    return {
        "currency": currency,
        "count": len(products),
        "categories": by_category,
        "products": products,
    }


def _parse_items(raw: str | list | dict) -> list[dict]:
    """Read the items the agent chose.

    Models are inconsistent here: some send a JSON string, some send the array
    itself, and some wrap it in a code fence. All three are accepted rather than
    failing a shopper's turn over a formatting detail.
    """
    items = raw
    if isinstance(items, str):
        text = items.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        items = json.loads(text)
    if isinstance(items, dict):
        items = items.get("items") or [items]
    if not isinstance(items, list):
        raise ValueError("items must be a JSON array")
    return [i for i in items[:MAX_OUTFIT_ITEMS] if isinstance(i, dict)]


def _match_variant(product: dict, want_colour: str | None, want_size: str | None) -> dict | None:
    """Pick the variant matching the requested colour and size, preferring one in stock."""

    def matches(variant: dict) -> bool:
        if want_colour:
            value = _colour_of(variant)
            if not value or value.casefold() != want_colour.casefold():
                return False
        if want_size:
            value = _option_value(variant, "Size")
            if not value or value.casefold() != want_size.casefold():
                return False
        return True

    candidates = [v for v in product["variants"]["nodes"] if matches(v)]
    if not candidates:
        return None
    return next((v for v in candidates if v["availableForSale"]), candidates[0])


async def build_outfit(items: str | list, budget: float | None = None) -> dict:
    """Price a chosen look exactly and name the variants the frontend should add."""
    try:
        requested = _parse_items(items)
    except (ValueError, json.JSONDecodeError) as exc:
        return {"error": f"Could not read the outfit items: {exc}", "expected_format": OUTFIT_FORMAT}

    handles = [str(i.get("handle", "")).strip() for i in requested if i.get("handle")]
    if not handles:
        return {"error": "No product handles given.", "expected_format": OUTFIT_FORMAT}

    currency = (await shop_info())["currency"]
    products = {p["handle"]: p for p in await _active_products(handles)}

    chosen: list[dict] = []
    problems: list[dict] = []
    total = Decimal("0")

    for item in requested:
        handle = str(item.get("handle", "")).strip()
        colour = item.get("color") or item.get("colour") or None
        size = item.get("size") or None
        try:
            quantity = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1

        product = products.get(handle)
        if product is None:
            problems.append({"handle": handle, "reason": "not_found_or_not_for_sale"})
            continue

        variant = _match_variant(product, colour, size)
        if variant is None:
            options = _options_of(product)
            problems.append(
                {
                    "handle": handle,
                    "title": product["title"],
                    "reason": "no_variant_for_that_choice",
                    "available_colors": options.get("Color") or options.get("Colour") or [],
                    "available_sizes": options.get("Size") or [],
                }
            )
            continue

        if not variant["availableForSale"]:
            problems.append(
                {
                    "handle": handle,
                    "title": product["title"],
                    "option": variant["title"],
                    "reason": "out_of_stock",
                }
            )
            continue

        unit_price = _money(variant["price"])
        line_total = unit_price * quantity
        total += line_total
        variant_id = variant.get("legacyResourceId")
        chosen.append(
            {
                "handle": handle,
                "product_id": product.get("legacyResourceId"),
                "variant_id": variant_id,
                "title": product["title"],
                "category": _category(product["title"], product.get("productType")),
                "option": None if variant["title"] == "Default Title" else variant["title"],
                "sku": variant.get("sku") or None,
                "unit_price": float(unit_price),
                "quantity": quantity,
                "line_total": float(line_total),
                # The variant's own photo when it has one, so a pink shoe shows pink.
                "image": variant_image(variant) or product_image(product),
                "url": product_url(product, variant_id),
            }
        )

    result: dict = {
        "outfit": chosen,
        "item_count": len(chosen),
        "currency": currency,
        "total": float(total),
        "problems": problems,
    }

    if budget:
        allowance = _money(budget)
        result["budget"] = float(allowance)
        result["within_budget"] = total <= allowance
        difference = allowance - total
        result["remaining" if difference >= 0 else "over_by"] = float(abs(difference))

    # The frontend adds the look to the bag itself, so it just needs the variants.
    result["cart_items"] = [
        {"variant_id": c["variant_id"], "quantity": c["quantity"]} for c in chosen
    ]
    return result


__all__ = ["OUTFIT_FORMAT", "ShopifyError", "browse_catalogue", "build_outfit"]
