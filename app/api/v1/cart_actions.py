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


def _clauses(text: str) -> list[str]:
    """The parts of the message that are not questions, with negated verbs struck out."""
    parts = (p.strip() for p in re.split(r"[.?!\n]+", text))
    return [_NEGATED_RE.sub(" ", p) for p in parts if p and not _QUESTION_RE.search(p)]


# The reply asks them to choose something - a size, a colour, which one.
_CHOOSING_RE = re.compile(r"\b(?:sizes?|colou?rs?|which\s+(?:one|of)|shade)\b", re.I)
_ASKED_RE = re.compile(r"[^.!?\n]*\?")


def _asks_to_choose(reply: str) -> bool:
    return any(_CHOOSING_RE.search(q) for q in _ASKED_RE.findall(reply or ""))


def cart_action(
    message: str,
    issued: list[dict] | None = None,
    *,
    bag_empty: bool = False,
    waiting: bool = False,
) -> str | None:
    """REMOVE_FROM_CART, ADD_PREVIOUS, CHECKOUT, CHECKOUT_FROM_EXISTING, OPEN_CART, or None.

    Only what the shopper chose goes in the bag, so nothing is added here that
    the agent's add_to_cart did not add, in the exact variants it resolved: the
    widget adding the products it showed would pick a size nobody asked for.

    issued: the ``action`` dicts the agent's own tools produced this turn. Checking
    out wins over adding - "add these and checkout" is a checkout with them in it.
    bag_empty: the widget sent a cart with nothing in it.
    waiting: the agent's add_to_cart or remove_from_cart changed nothing and is
    asking a question.
    """
    if waiting:
        return None
    text = " ".join((message or "").lower().replace("’", "'").split())
    clauses = _clauses(text)
    issued = issued or []
    # Taking something out leads: it carries the page to go to next, if any.
    if any(a.get("type") == "remove_from_cart" and a.get("items") for a in issued):
        return REMOVE_FROM_CART
    added = any(a.get("type") == "add_to_cart" and a.get("items") for a in issued)

    checkout = any(_CHECKOUT_RE.search(c) for c in clauses) or any(
        a.get("type") == "redirect" and a.get("page") == "checkout" for a in issued
    )
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
    if _asks_to_choose(reply):
        if word == CHECKOUT:
            word = ADD_PREVIOUS
        elif word not in (ADD_PREVIOUS, REMOVE_FROM_CART):
            word = None
    event = payload(word, issued)
    if event and _asks_to_choose(reply):
        for key in ("page", "url", "absolute_url"):
            event.pop(key, None)
    return event
