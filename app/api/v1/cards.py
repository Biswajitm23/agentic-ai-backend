"""Product cards for the chat clients.

A tool result is JSON meant for the model; a storefront needs a picture, a price
and somewhere to click. This turns one into the other, and is shared by every
chat endpoint so a client gets the same cards whether it streams the reply or
takes it in one piece.
"""

import json

# Tools whose result a client can render as cards, and the key it arrives under.
CARD_TOOLS = {
    "search_products": "products",
    "browse_catalogue": "products",
    "build_outfit": "outfit",
}
MAX_CARDS = 12


def _card(item: dict) -> dict:
    """The fields a storefront needs to draw a product and link to it."""
    return {
        "product_id": item.get("product_id"),
        "variant_id": item.get("variant_id"),
        "title": item.get("title"),
        "option": item.get("option"),
        "price": item.get("unit_price", item.get("price_from")),
        "currency": item.get("currency"),
        "image": item.get("image"),
        "url": item.get("url"),
    }


def cards_from(tool_name: str, output: str | None) -> dict | None:
    """Renderable cards from one tool result, or None when there are none."""
    if not output or tool_name not in CARD_TOOLS:
        return None
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None

    currency = data.get("currency")

    def card(item: dict) -> dict:
        return {**_card(item), "currency": item.get("currency") or currency}

    if tool_name == "build_outfit":
        items = data.get("outfit") or []
        if not items:
            return None
        return {
            "items": [card(i) for i in items[:MAX_CARDS]],
            "currency": currency,
            "total": data.get("total"),
            "budget": data.get("budget"),
            "within_budget": data.get("within_budget"),
            "cart_items": data.get("cart_items") or [],
        }

    items = data.get("products") or []
    if not items:
        return None
    return {"items": [card(i) for i in items[:MAX_CARDS]], "currency": currency}


class CardCollector:
    """Gathers cards across a turn so they can be sent mid-stream and at the end.

    A turn may call several tools; the last product result is the one the reply
    is actually about, so later cards replace earlier ones of the same kind.
    """

    def __init__(self) -> None:
        self.products: dict | None = None
        self.outfit: dict | None = None

    def take(self, tool_name: str, output: str | None) -> tuple[str, dict] | None:
        """Record a tool result. Returns (event_name, payload) when it had cards."""
        cards = cards_from(tool_name, output)
        if cards is None:
            return None
        name = CARD_TOOLS[tool_name]
        setattr(self, name, cards)
        return name, cards

    def as_dict(self) -> dict:
        """Whatever was collected, for the final payload."""
        out: dict = {}
        if self.products is not None:
            out["products"] = self.products
        if self.outfit is not None:
            out["outfit"] = self.outfit
        return out
