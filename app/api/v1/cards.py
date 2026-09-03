"""Product cards for the chat clients.

A tool result is JSON meant for the model; a storefront needs a picture, a price
and somewhere to click. This turns one into the other, and is shared by every
chat endpoint so a client gets the same cards whether it streams the reply or
takes it in one piece.
"""

import json
import re

# Tools whose result a client can render as cards, and the key it arrives under.
CARD_TOOLS = {
    "search_products": "products",
    "browse_catalogue": "products",
    "recommend_for_me": "products",
    "build_outfit": "outfit",
    "get_my_order_history": "orders",
    "check_order_status": "orders",
    # Not a card - a set of buttons. Same idea though: the shopper should be
    # tapping a choice, not reading the agent recite seven of them.
    "request_order_change": "choices",
}
MAX_CARDS = 12


_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say nothing about which product this is.
_NOISE = {"the", "and", "for", "with", "in", "of", "a", "an", "kids", "girls", "boys"}
# Used only when a title has no word of its own to be recognised by.
_MENTION_RATIO = 0.5


def _stem(word: str) -> str:
    """Fold simple plurals so "Mary Janes" still finds "Mary Jane"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) > 2 and w not in _NOISE}


def keep_mentioned(items: list[dict], reply: str) -> list[dict]:
    """The products the reply actually talks about.

    Prose shortens titles - "Catherine Gingham Embroidered Sleeveless Trapeze
    Dress" becomes "the Catherine Gingham dress", "Leather Mary Jane Shoes"
    becomes "the Mary Janes" - so a product counts as mentioned when the reply
    uses a word that belongs to it alone.

    Matching on any shared word would be wrong in the other direction: with
    "Leather T Bar Baby Shoes" in the reply, "leather" and "shoes" must not drag
    the Mary Janes in beside it.
    """
    said = _words(reply)
    title_words = [(item, _words(item.get("title") or "")) for item in items]

    frequency: dict[str, int] = {}
    for _, words in title_words:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1

    kept = []
    for item, words in title_words:
        if not words:
            continue
        distinctive = {w for w in words if frequency.get(w, 1) == 1}
        if distinctive:
            if distinctive & said:
                kept.append(item)
        elif len(words & said) / len(words) >= _MENTION_RATIO:
            # Nothing sets this title apart, so fall back to how much of it appears.
            kept.append(item)
    return kept


def _card(item: dict) -> dict:
    """The fields a storefront needs to draw a product and link to it."""
    return {
        "product_id": item.get("product_id"),
        "variant_id": item.get("variant_id"),
        "title": item.get("title"),
        "option": item.get("option"),
        # Tools name this differently: a unit price, a "from" price, or a plain one.
        "price": next(
            (item[k] for k in ("unit_price", "price_from", "price") if item.get(k) is not None),
            None,
        ),
        "currency": item.get("currency"),
        "image": item.get("image"),
        "url": item.get("url"),
        # Only recommendations set this; it is why the product was suggested.
        "because": item.get("because"),
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

    if tool_name in ("get_my_order_history", "check_order_status"):
        # check_order_status returns one order; the history tool returns a list.
        orders = data.get("orders") if "orders" in data else ([data] if data.get("found") else [])
        orders = [o for o in (orders or []) if o.get("order_number")]
        if not orders:
            return None
        return {
            "orders": [
                {
                    "order_number": o.get("order_number"),
                    "placed_on": o.get("placed_on"),
                    "status": o.get("status"),
                    "status_meaning": o.get("status_meaning"),
                    "total": o.get("total"),
                    "currency": o.get("currency"),
                    "tracking": o.get("tracking") or [],
                    "items": [
                        _card(i) | {"quantity": i.get("quantity"), "line_total": i.get("line_total")}
                        for i in (o.get("items") or [])
                    ],
                }
                for o in orders[:MAX_CARDS]
            ]
        }

    if tool_name == "choices" or tool_name == "request_order_change":
        if not data.get("eligible"):
            return None
        options = [
            {"code": r["code"], "label": r["label"]}
            for r in (data.get("reasons") or [])
            if r.get("code") and r.get("label")
        ]
        if not options:
            return None
        return {
            "options": options,
            "action": data.get("action"),
            "order_number": data.get("order_number"),
            # There is always a way out of the list.
            "allow_free_text": True,
        }

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
        self.orders: dict | None = None
        self.choices: dict | None = None

    def take(self, tool_name: str, output: str | None) -> tuple[str, dict] | None:
        """Record a tool result. Returns (event_name, payload) when it had cards."""
        cards = cards_from(tool_name, output)
        if cards is None:
            return None
        name = CARD_TOOLS[tool_name]
        setattr(self, name, cards)
        return name, cards

    def finalise(self, reply: str) -> None:
        """Reconcile the cards with the answer the shopper actually reads.

        A tool hands back everything it found - the whole catalogue, ten search
        results - and the agent then picks a few to talk about. Sending all of
        them would show five products under a list of three. So once the reply
        exists, keep only what it mentions.

        An outfit is exempt: it *is* the answer, priced and totalled, so it is
        sent whole, and the browse that fed it is dropped as noise.
        """
        # An outfit or an order listing IS the answer, so it is sent whole and any
        # browse that fed it is dropped as noise.
        if self.outfit is not None or self.orders is not None:
            self.products = None
        if self.outfit is not None or self.orders is not None:
            return
        # Choices are never reconciled against the wording: the whole point is
        # that they do not depend on what the agent chose to say.
        if self.products is None:
            return
        items = self.products.get("items") or []
        kept = keep_mentioned(items, reply)
        # Never leave a shopper with nothing to click because the wording drifted.
        if kept:
            self.products = {**self.products, "items": kept}

    def as_dict(self) -> dict:
        """Whatever was collected, for the final payload."""
        out: dict = {}
        if self.products is not None:
            out["products"] = self.products
        if self.outfit is not None:
            out["outfit"] = self.outfit
        if self.orders is not None:
            out["orders"] = self.orders
        if self.choices is not None:
            out["choices"] = self.choices
        return out
