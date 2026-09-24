"""What the shopper wants done with their bag, as one word the widget acts on.

Only what the shopper chose goes in the bag. The widget never adds the products
it showed on its own - it could only guess a size - so every addition is the
agent's add_to_cart, in the exact variants it resolved once the shopper had
named the size and colour. payload() puts those variant ids, and the page to go
to, in the same one ``actions`` event.

Checking out is read off the shopper's own words as well, the way the requested
count is: the model is not relied on to report an intent it may not act on.
"""

import re

from app.services.shopify_client import store_domain

ADD_PREVIOUS = "add previous products in cart"
CHECKOUT = "checkout"
CHECKOUT_FROM_EXISTING = "checkout from existing"
# Only ever the agent's own doing - "take me to my bag" - never read off the words.
OPEN_CART = "open cart"
# Also only the agent's doing: remove_from_cart found the exact lines in the bag.
REMOVE_FROM_CART = "remove from cart"
# ...and edit_cart: a line's new quantity, or the same product in another size or colour.
EDIT_FROM_CART = "edit from cart"

# Where each word leaves the shopper; adding alone keeps them on the page.
_PAGE = {CHECKOUT: "checkout", CHECKOUT_FROM_EXISTING: "checkout", OPEN_CART: "cart"}

_CHECKOUT_WORD = r"(?:check-?\s?out|pay(?:ment)?)"

# A clause that asks about the cart rather than asking us to act on it - "how do
# I checkout?", "does checkout take UPI?". "Can you..." stays: that is a request.
_QUESTION_RE = re.compile(
    r"^(?:how|what|where|when|why|which|is|are|does|do|did|was|were|has|have)\b"
    r"|\bhow\s+(?:do|does|can|to|would|should)\b"
)

# "don't add these", "not ready to checkout" - the verb is struck out to the end
# of its clause, so "don't add these, just checkout" is still a checkout.
_NEGATED_RE = re.compile(
    r"\b(?:don't|dont|do\s+not|not|never|won't|wont)\s+"
    r"(?:(?:want|wanna|need|going|ready|like|yet)\s+)?(?:to\s+)?"
    r"(?:add|put|check-?\s?out|pay|buy|place)\b[^,.;!?]*"
)

_CHECKOUT_RE = re.compile(
    r"\bcheck-?out\b"
    # Two words is also "look at": "check out this dress", "check out the sale".
    r"|\bcheck\s+out\b(?!\s+(?:this|that|these|those|the|our|some|a|an|more|other|your|what)\b)"
    r"|\b(?:place|confirm)\s+(?:my|the|this|an?)\s+order\b"
    r"|\b(?:buy|pay)\s+now\b"
    r"|\bmake\s+(?:the\s+|a\s+)?payment\b"
    rf"|\b(?:proceed|go|move|ready|want|like|wanna)\s+(?:on\s+)?to\s+(?:the\s+)?{_CHECKOUT_WORD}\b"
    rf"|\btake\s+me\s+to\s+(?:the\s+)?{_CHECKOUT_WORD}\b"
)


def wants_checkout(message: str) -> bool:
    """Whether the shopper's own words ask to check out - "checkout", "buy now", "pay"."""
    text = " ".join((message or "").lower().replace("’", "'").split())
    return any(_CHECKOUT_RE.search(c) for c in _clauses(text))


def _clauses(text: str) -> list[str]:
    """The parts of the message that are not questions, with negated verbs struck out."""
    parts = (p.strip() for p in re.split(r"[.?!\n]+", text))
    return [_NEGATED_RE.sub(" ", p) for p in parts if p and not _QUESTION_RE.search(p)]


# The reply asks them to choose something - a size, a colour, which one - with
# a question mark or without: "let me know the size you'd like".
_CHOOSING_RE = re.compile(r"\b(?:sizes?|colou?rs?|which\s+(?:one|of)|shade)\b", re.I)
_ASKED_RE = re.compile(r"[^.!?\n]*\?|[^.!?\n]*\b(?:let me know|tell me|please (?:choose|pick|select))\b[^.!?\n]*",
                       re.I)


def _asks_to_choose(reply: str) -> bool:
    return any(_CHOOSING_RE.search(q) for q in _ASKED_RE.findall(reply or ""))


def cart_action(
    message: str,
    issued: list[dict] | None = None,
    *,
    bag_empty: bool = False,
    waiting: bool = False,
) -> str | None:
    """EDIT_FROM_CART, REMOVE_FROM_CART, ADD_PREVIOUS, CHECKOUT, CHECKOUT_FROM_EXISTING, OPEN_CART, or None.

    Only what the shopper chose goes in the bag, so nothing is added here that
    the agent's add_to_cart did not add, in the exact variants it resolved: the
    widget adding the products it showed would pick a size nobody asked for.

    issued: the ``action`` dicts the agent's own tools produced this turn. Checking
    out wins over adding - "add these and checkout" is a checkout with them in it.
    bag_empty: the widget sent a cart with nothing in it.
    waiting: a question about the bag is still open - decide() keeps the
    shopper on the page, but what already went in still goes.
    """
    issued = issued or []
    # Changing lines already in the bag leads: an edit carries any removal and
    # any addition made beside it, and the page to go to next, if any.
    if any(a.get("type") == "edit_cart" and (a.get("items") or a.get("add_items")) for a in issued):
        return EDIT_FROM_CART
    if any(a.get("type") == "remove_from_cart" and a.get("items") for a in issued):
        return REMOVE_FROM_CART
    added = any(a.get("type") == "add_to_cart" and a.get("items") for a in issued)

    # Checkout is the agent's decision - its go_to_checkout call - never the
    # word "checkout" found in the message.
    checkout = any(a.get("type") == "redirect" and a.get("page") == "checkout" for a in issued)
    if checkout:
        if added:
            return CHECKOUT
        # Nothing went in this turn, so the bag goes as it is - and an empty bag
        # is an empty checkout page.
        return None if bag_empty else CHECKOUT_FROM_EXISTING
    if added:
        return ADD_PREVIOUS
    if any(a.get("type") == "redirect" and a.get("page") == "cart" for a in issued):
        return OPEN_CART
    return None


def payload(word: str | None, issued: list[dict] | None = None) -> dict | None:
    """The ``actions`` event: the word, with the exact instructions behind it.

    items - the variants the agent put in the bag this turn, as {variant_id,
            quantity}, in the size and colour the shopper chose: add exactly
            these first. Always there for "add previous products in cart" and
            "checkout"; "checkout from existing" is the bag as it stands.
    url   - where to send the shopper once the bag is right; absent, stay put.
    """
    if not word:
        return None
    if word == REMOVE_FROM_CART:
        return _removal(issued or [])
    if word == EDIT_FROM_CART:
        return _edit(issued or [])
    event: dict = {"action": word}
    if word != CHECKOUT_FROM_EXISTING:
        items = [line for a in issued or [] if a.get("type") == "add_to_cart"
                 for line in a.get("items") or []]
        if items:
            event["items"] = items
    page = _PAGE.get(word)
    if page:
        event["page"] = page
        event["url"] = f"/{page}"
        event["absolute_url"] = f"https://{store_domain()}/{page}"
    return event


def _removal(issued: list[dict]) -> dict:
    """The removal event: only the lines coming out, then anything else this turn did.

    items     - {variant_id, title, option, quantity, new_quantity}: take quantity
                out of that line, leaving new_quantity (0 = the line goes).
    add_items - a swap ("the blue one instead"): {variant_id, quantity} to add after.
    url       - where to go afterwards, when they also asked for checkout or the bag.
    """
    event: dict = {
        "action": REMOVE_FROM_CART,
        "items": [line for a in issued if a.get("type") == "remove_from_cart" for line in a.get("items") or []],
    }
    adding = [line for a in issued if a.get("type") == "add_to_cart" for line in a.get("items") or []]
    if adding:
        event["add_items"] = adding
    page = next((a.get("page") for a in issued if a.get("type") == "redirect"), None)
    if page in ("checkout", "cart"):
        event["page"] = page
        event["url"] = f"/{page}"
        event["absolute_url"] = f"https://{store_domain()}/{page}"
    return event


def _edit(issued: list[dict]) -> dict:
    """The edit event: every line of the bag that changes, and anything added beside it.

    items     - {variant_id, title, option, color, size, quantity_before,
                new_quantity}: set that line to new_quantity (0 = the line goes).
                A removal made in the same turn is one of these, at 0.
    add_items - {variant_id, quantity} to add after: the new size or colour of
                a line that changed, or anything add_to_cart put in beside it.
    url       - where to go afterwards, when they also asked for checkout or the bag.
    """
    items = [line for a in issued if a.get("type") == "edit_cart" for line in a.get("items") or []]
    for a in issued:
        if a.get("type") == "remove_from_cart":
            for line in a.get("items") or []:
                items.append({"variant_id": line["variant_id"], "title": line.get("title"),
                              "option": line.get("option"), "color": line.get("color"),
                              "size": line.get("size"),
                              "quantity_before": line["quantity"] + line["new_quantity"],
                              "new_quantity": line["new_quantity"]})
    event: dict = {"action": EDIT_FROM_CART, "items": items}
    adding = [line for a in issued if a.get("type") in ("edit_cart", "add_to_cart")
              for line in a.get("add_items" if a.get("type") == "edit_cart" else "items") or []]
    if adding:
        event["add_items"] = adding
    page = next((a.get("page") for a in issued if a.get("type") == "redirect"), None)
    if page in ("checkout", "cart"):
        event["page"] = page
        event["url"] = f"/{page}"
        event["absolute_url"] = f"https://{store_domain()}/{page}"
    return event


def decide(
    message: str,
    issued: list[dict] | None = None,
    *,
    bag_empty: bool = False,
    waiting: bool = False,
    reply: str = "",
) -> dict | None:
    """The one ``actions`` event for this turn, or None.

    A reply asking them to pick something - a size, a colour, which one - is no
    moment to leave the page, so it goes nowhere. What already went in or came
    out still stands: "Added the dress - which size for the shoes?" adds the dress.
    """
    word = cart_action(message, issued, bag_empty=bag_empty, waiting=waiting)
    # Still asking - the next dress's size, say - so nobody leaves the page yet:
    # checkout comes once the last one is in.
    staying = waiting or _asks_to_choose(reply)
    if staying:
        if word == CHECKOUT:
            word = ADD_PREVIOUS
        elif word not in (ADD_PREVIOUS, REMOVE_FROM_CART, EDIT_FROM_CART):
            word = None
    event = payload(word, issued)
    if event and staying:
        for key in ("page", "url", "absolute_url"):
            event.pop(key, None)
    return event


# ── The products each action acts on ───────────────────────────────────────
# An action is always about products, so it names them: the widget should never
# have to work out from elsewhere which ones "add", "checkout" or "remove" meant.

_SHOWN_AS = {
    ADD_PREVIOUS: ("Added to your bag", "added"),
    CHECKOUT: ("Checking out", "checkout"),
    CHECKOUT_FROM_EXISTING: ("Checking out", "checkout"),
    OPEN_CART: ("Your bag", "cart"),
    REMOVE_FROM_CART: ("Removed from your bag", "removed"),
    EDIT_FROM_CART: ("Your bag was updated", "edited"),
}
# color and size are the variant's own: which one the action means, never a guess.
_CARD_FIELDS = ("product_id", "variant_id", "title", "option", "color", "size", "image", "url", "currency")


def _product(card: dict, change: str, *, price_is_line: bool = False) -> dict:
    """One product as every action carries it: price per unit, with the line's total."""
    quantity = int(card.get("quantity") or 1)
    price = card.get("price")
    if price is not None and price_is_line:
        price = round(price / quantity, 2)          # the bag reports a line's total
    product = {k: card.get(k) for k in _CARD_FIELDS}
    product["options"] = card.get("options") or {}
    product.update(quantity=quantity, price=price,
                   line_total=round(price * quantity, 2) if price is not None else None,
                   change=change)
    for key in ("quantity_before", "new_quantity"):
        if key in card:
            product[key] = card[key]
    return product


def _checking_out(bag: list[dict], added: list[dict]) -> list[dict]:
    """Everything that goes to checkout: the bag, with what was just added folded in."""
    lines: dict[str, dict] = {}
    for i, line in enumerate(bag):
        lines[str(line.get("variant_id") or f"line-{i}")] = _product(line, "in_bag", price_is_line=True)
    for card in added:
        key = str(card.get("variant_id"))
        if key in lines:
            # Already in the bag: one card, with the new total.
            line = lines[key]
            line["quantity"] += int(card.get("quantity") or 1)
            line["line_total"] = round(line["price"] * line["quantity"], 2) if line["price"] is not None else None
            line["change"] = "added"
        else:
            lines[key] = _product(card, "added")
    return list(lines.values())


def with_products(event: dict | None, changed: list[dict], bag: list[dict]) -> dict | None:
    """The event with the products it acts on under ``products``.

    changed - the cards of what this turn added or removed.
    bag     - the shopper's cart lines as the widget sent them, with imagery.
    """
    if not event:
        return event
    word = event["action"]
    if word in (ADD_PREVIOUS, REMOVE_FROM_CART, EDIT_FROM_CART):
        products = [_product(c, c.get("change") or "added") for c in changed]
    elif word == CHECKOUT:
        products = _checking_out(bag, [c for c in changed if c.get("change") == "added"])
    else:                                            # the bag as it stands
        products = [_product(line, "in_bag", price_is_line=True) for line in bag]
    return {**event, "products": products}


def products_event(event: dict | None) -> dict | None:
    """The products SSE payload for an action turn: the same products the action names."""
    if not event or not event.get("products"):
        return None
    heading, layout = _SHOWN_AS.get(event["action"], ("Your bag", "cart"))
    if event.get("add_items") and event["action"] != EDIT_FROM_CART:
        heading, layout = "Your bag was updated", "bag_update"
    products = event["products"]
    return {"items": products, "currency": next((p["currency"] for p in products if p.get("currency")), None),
            "heading": heading, "layout": layout}
