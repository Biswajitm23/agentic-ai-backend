"""Follow-on prompts a shopper can tap instead of typing.

Every suggestion is built from something the store actually stocks, and the text
sent on a tap is a phrasing the agent already resolves. That is the whole point:
a chip the model invented could read beautifully - "browse our dining range" -
and then land on nothing, which is worse than offering no chip at all.

Order is deliberate. What the shopper is looking at leads, the rest of the shop
follows, and the catch-all sits last where it costs nothing.
"""

import logging

from app.services import shopify_storefront

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4
CATEGORY_POOL = 12       # categories considered before picking
BEST_SELLERS = {"label": "Show best sellers", "prompt": "What are your best sellers?"}

# Plurals the -s rule gets wrong, and names that are plural already.
_IRREGULAR = {"dress": "dresses", "blouse": "blouses", "bib": "bibs", "hairband": "hairbands"}
_ALREADY_PLURAL = {"shoes", "boots", "trousers", "shorts", "sunglasses", "mittens", "socks",
                   "toys", "plants", "essentials", "aminos"}


def plural(name: str) -> str:
    """"Dress" -> "Dresses", "Shoes" -> "Shoes". Chips read as a shelf, not a SKU."""
    lowered = name.strip().lower()
    if lowered in _ALREADY_PLURAL:
        return name
    if lowered in _IRREGULAR:
        return _IRREGULAR[lowered].title()
    if lowered.endswith(("s", "x", "ch", "sh")):
        return f"{name}es"
    return f"{name}s"


def _category_chip(entry: dict) -> dict:
    name = plural(entry["name"])
    return {"label": f"Explore {name}", "prompt": f"Show me {name.lower()}",
            "kind": "category", "id": entry.get("id")}


def _collection_chip(entry: dict) -> dict:
    title = entry.get("name") or entry.get("title") or ""
    return {"label": f"Shop {title}", "prompt": f"What is in {title}?",
            "kind": "collection", "id": entry.get("handle") or entry.get("id")}


async def for_turn(shown_category: dict | None = None, limit: int = MAX_SUGGESTIONS) -> list[dict]:
    """Chips to offer after a reply.

    ``shown_category`` is whatever the shopper is already looking at; it is left
    out, since offering someone the shelf they are standing in front of is not a
    suggestion. Everything else is ranked by how much it holds, because a fuller
    category is a better door into the shop than a thinner one.
    """
    seen_id = (shown_category or {}).get("id")
    seen_name = ((shown_category or {}).get("name") or "").strip().lower()
    chips: list[dict] = []

    try:
        listed = (await shopify_storefront.categories(CATEGORY_POOL))["categories"]
    except Exception:  # noqa: BLE001 - chips are a nicety, never a reason to fail a reply
        logger.warning("Could not build category suggestions", exc_info=True)
        listed = []

    for entry in listed:
        if len(chips) >= limit - 1:
            break
        if entry.get("id") == seen_id or entry["name"].strip().lower() == seen_name:
            continue
        chips.append(_category_chip(entry))

    # One collection alongside the plain categories: it is the merchandised door,
    # and it reads differently enough that the row does not look like one list.
    try:
        collections = (await shopify_storefront.collections(6))["collections"]
        for entry in collections:
            handle = entry.get("handle") or entry.get("id")
            if handle and handle != seen_id and len(chips) < limit:
                chips.append(_collection_chip(entry))
                break
    except Exception:  # noqa: BLE001
        logger.warning("Could not build a collection suggestion", exc_info=True)

    if len(chips) < limit:
        chips.append(dict(BEST_SELLERS))
    return chips[:limit]


async def for_welcome(limit: int = MAX_SUGGESTIONS) -> list[dict]:
    """Chips for the opening screen, where nothing has been shown yet."""
    return await for_turn(None, limit)
