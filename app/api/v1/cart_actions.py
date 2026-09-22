"""What the shopper wants done with their bag, as one word the widget acts on.

The shopper asked to add what they were shown, to check out with it, or to check
out with only what is already in the bag. The widget holds the cards it drew, so
it knows which products "these" are. When the agent itself called add_to_cart or
go_to_checkout, payload() adds its exact instructions - these variant ids, this
page - to the same one ``actions`` event.

Read off the shopper's own words first, the way the requested count is: the model
is not relied on to report an intent it may not act on. What the agent did this
turn fills the gap for the replies that name no action - "yes please" to "shall I
add the look to your bag?".
"""

import re

from app.services.shopify_client import store_domain

ADD_PREVIOUS = "add previous products in cart"
CHECKOUT = "checkout"
CHECKOUT_FROM_EXISTING = "checkout from existing"
# Only ever the agent's own doing - "take me to my bag" - never read off the words.
OPEN_CART = "open cart"

# Where each word leaves the shopper; adding alone keeps them on the page.
_PAGE = {CHECKOUT: "checkout", CHECKOUT_FROM_EXISTING: "checkout", OPEN_CART: "cart"}

_CART = r"(?:cart|bag|basket|trolley)"
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

_ADD_RE = re.compile(
    # "add all these products in my cart", "put them in my bag", "add to cart"
    rf"\b(?:add|put|pop|throw)\b(?:\s+[\w'-]+){{0,6}}?\s+(?:in|into|to|onto)\s+"
    rf"(?:my\s+|the\s+|our\s+)?(?:shopping\s+)?{_CART}\b"
    # "add them", "add these", "add the look"
    r"|\badd\s+(?:it|them|these|those|this|that|all|everything|both|the\s+(?:look|outfit|lot|set))\b"
)

# They do not want what was shown - checked across the whole message, since "Just
# checkout. I don't need the above." puts it in a sentence of its own.
_REJECT_RE = re.compile(
    r"\b(?:don't|dont|do\s+not|never)\s+(?:need|want|like)\b(?!\s+to\b)"
    r"|\b(?:don't|dont|do\s+not|never)\s+(?:add|include|put)\b"
    r"|\bno\s+need\b"
    r"|\bwithout\s+(?:adding|these|those|them|the\s+(?:above|ones|products?|items?|suggest\w*|recommend\w*))\b"
    r"|\b(?:skip|forget|leave|ignore|drop)\s+(?:about\s+)?(?:these|those|them|it|that|this"
    r"|the\s+(?:above|rest|ones|products?|items?|suggest\w*|recommend\w*|outfit|look))\b"
    r"|\b(?:not|none\s+of)\s+(?:these|those|them|the\s+(?:above|ones|products?|items?))\b"
    # Only what is already there: "what's in my cart", "my existing bag", "I already added them"
    rf"|\bwhat(?:ever)?(?:'s|s|\s+is|\s+i\s+(?:already\s+)?(?:have|had|got))\s+(?:already\s+)?in\s+(?:my|the)\s+{_CART}\b"
    r"|\bwith\s+what(?:ever)?\s+i\s+(?:already\s+)?(?:have|had|got)\b"
    rf"|\b(?:existing|current)\s+(?:{_CART}|items?|products?)\b"
    rf"|\balready\s+(?:have\s+)?in\s+(?:my|the)\s+{_CART}\b"
    r"|\balready\s+(?:added|put|have)\b"
    # "just checkout" - nothing else, the shown products included
    rf"|\b(?:just|only|straight|directly)\s+(?:(?:go|take\s+me|proceed)\s+)?(?:to\s+)?(?:the\s+)?{_CHECKOUT_WORD}\b"
)


def _clauses(text: str) -> list[str]:
    """The parts of the message that are not questions, with negated verbs struck out."""
    parts = (p.strip() for p in re.split(r"[.?!\n]+", text))
    return [_NEGATED_RE.sub(" ", p) for p in parts if p and not _QUESTION_RE.search(p)]


def cart_action(
    message: str,
    issued: list[dict] | None = None,
    *,
    bag_empty: bool = False,
    waiting: bool = False,
) -> str | None:
    """ADD_PREVIOUS, CHECKOUT, CHECKOUT_FROM_EXISTING, OPEN_CART, or None.

    issued: the ``action`` dicts the agent's own tools produced this turn. Checking
    out wins over adding - "add these and checkout" is a checkout with them in it.
    bag_empty: the widget sent a cart with nothing in it.
    waiting: the agent's add_to_cart put nothing in and is asking a question, so
    leaving the page now would walk out on it.
    """
    if waiting:
        return None
    text = " ".join((message or "").lower().replace("’", "'").split())
    clauses = _clauses(text)
    issued = issued or []
    adding = any(_ADD_RE.search(c) for c in clauses) or any(a.get("type") == "add_to_cart" for a in issued)

    checkout = any(_CHECKOUT_RE.search(c) for c in clauses) or any(
        a.get("type") == "redirect" and a.get("page") == "checkout" for a in issued
    )
    if checkout:
        # An empty bag with nothing going into it is an empty checkout page.
        if bag_empty and (not adding or _REJECT_RE.search(text)):
            return None
        return CHECKOUT_FROM_EXISTING if _REJECT_RE.search(text) else CHECKOUT
    if adding:
        return ADD_PREVIOUS
    if any(a.get("type") == "redirect" and a.get("page") == "cart" for a in issued):
        return OPEN_CART
    return None


def payload(word: str | None, issued: list[dict] | None = None) -> dict | None:
    """The ``actions`` event: the word, with the exact instructions behind it.

    items - the variants the agent put in the bag this turn, as {variant_id,
            quantity}: add exactly these first. Absent, "add previous products"
            and "checkout" mean the products the widget showed.
    url   - where to send the shopper once the bag is right; absent, stay put.
    """
    if not word:
        return None
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
