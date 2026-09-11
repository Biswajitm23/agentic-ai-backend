"""Follow-on prompts a shopper can tap instead of typing.

Every suggestion is built from something the store actually stocks, and the text
sent on a tap is a phrasing the agent already resolves. That is the whole point:
a chip the model invented could read beautifully - "browse our dining range" -
and then land on nothing, which is worse than offering no chip at all.

Order is deliberate. What the shopper is looking at leads, the rest of the shop
follows, and the catch-all sits last where it costs nothing.
"""

import logging
import re

from app.services import shopify_storefront

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4
CATEGORY_POOL = 12       # categories considered before picking
BEST_SELLERS = {"label": "Show best sellers", "prompt": "What are your best sellers?"}

# Plurals the -s rule gets wrong, and names that are plural already.
_IRREGULAR = {"dress": "dresses", "blouse": "blouses", "bib": "bibs", "hairband": "hairbands"}
_ALREADY_PLURAL = {"shoes", "boots", "trousers", "shorts", "sunglasses", "mittens", "socks",
                   "toys", "plants", "essentials", "aminos"}


# ── Answering our own question ─────────────────────────────────────────────
# When the agent asks the shopper something, a row of shelves is the wrong
# answer: they have just been asked what the occasion is, and "Explore Dresses"
# does not answer it. These chips reply to the question that was actually put.
#
# Each prompt is a sentence the agent already handles in the outfit flow, so a
# tap moves the conversation on rather than restarting it.

OCCASION_CHIPS = [
    {"label": "Birthday party", "prompt": "It is for a birthday party", "kind": "occasion"},
    {"label": "Wedding", "prompt": "It is for a wedding", "kind": "occasion"},
    {"label": "Christening", "prompt": "It is for a christening", "kind": "occasion"},
    {"label": "Everyday", "prompt": "Just for everyday wear", "kind": "occasion"},
]

WHO_CHIPS = [
    {"label": "For a girl", "prompt": "It is for a girl", "kind": "who"},
    {"label": "For a boy", "prompt": "It is for a boy", "kind": "who"},
]

# Ages spread across what the store sizes for, 0-3M up to 10Y. A shopper whose
# child is 4 types it; a chip for every year would bury the row.
AGE_CHIPS = [
    {"label": "Under 1", "prompt": "They are under 1", "kind": "age"},
    {"label": "3 years", "prompt": "They are 3 years old", "kind": "age"},
    {"label": "5 years", "prompt": "They are 5 years old", "kind": "age"},
    {"label": "8 years", "prompt": "They are 8 years old", "kind": "age"},
]

# No "no fixed budget" chip on purpose: with no number the look is never
# priced, budget stays in still_to_ask, and the agent asks for it again.
BUDGET_CHIPS = [
    {"label": "Under 10,000", "prompt": "My budget is 10000", "kind": "budget"},
    {"label": "Under 20,000", "prompt": "My budget is 20000", "kind": "budget"},
    {"label": "Under 30,000", "prompt": "My budget is 30000", "kind": "budget"},
]

# Asked in priority order: a compound question ("boy or girl, what occasion,
# which colour?") can only be answered one chip at a time, so the row answers
# the most useful part and the shopper types or taps the rest.
_QUESTION_KINDS = [
    ("occasion", re.compile(r"occasion|what is it for|what's it for|dressing up for", re.I)),
    ("who", re.compile(r"boy or (?:a )?girl|girl or (?:a )?boy|who is it for|who's it for", re.I)),
    ("age", re.compile(r"how old|what age|\bage\b", re.I)),
    ("budget", re.compile(r"budget|how much (?:would|do|can) you|spend", re.I)),
    ("colour", re.compile(r"colou?r", re.I)),
]


def _asks_a_question(reply: str) -> bool:
    return "?" in reply


_QUESTION_RE = re.compile(r"[^.!?\n]*\?")


def _questions_in(reply: str) -> str:
    """Only the sentences that ask something.

    The rest of a reply describes, and describing is not asking: "here is a look
    under your 20,000 budget - want me to add it to the bag?" asks about the bag,
    and matching the whole reply put budget chips under it.
    """
    return " ".join(_QUESTION_RE.findall(reply or ""))


def question_chips(reply: str, colours: list[str] | None = None) -> list[dict]:
    """Chips answering whatever the reply asked, or [] if it asked nothing."""
    if not reply or not _asks_a_question(reply):
        return []
    asked = _questions_in(reply)
    for kind, pattern in _QUESTION_KINDS:
        if not pattern.search(asked):
            continue
        if kind == "occasion":
            return [dict(c) for c in OCCASION_CHIPS]
        if kind == "who":
            return [dict(c) for c in WHO_CHIPS]
        if kind == "age":
            return [dict(c) for c in AGE_CHIPS]
        if kind == "budget":
            return [dict(c) for c in BUDGET_CHIPS]
        if colours:
            return [{"label": c, "prompt": f"In {c.lower()}", "kind": "colour"} for c in colours]
    return []


async def _stocked_colours(limit: int = 4) -> list[str]:
    """The colours the shop actually has most of, so a chip cannot miss."""
    from app.services import outfit

    try:
        catalogue = await outfit.browse_catalogue()
    except Exception:  # noqa: BLE001 - a chip row is never worth failing a reply for
        logger.warning("Could not read colours for suggestions", exc_info=True)
        return []
    counts: dict[str, int] = {}
    for product in catalogue.get("products") or []:
        for colour in product.get("colors") or []:
            counts[colour] = counts.get(colour, 0) + 1
    return sorted(counts, key=lambda c: -counts[c])[:limit]


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


async def for_turn(shown_category: dict | None = None, limit: int = MAX_SUGGESTIONS,
                   reply: str = "") -> list[dict]:
    """Chips to offer after a reply.

    A reply that asks the shopper something gets chips that answer it - that is
    what they are for at this point in the conversation.

    Otherwise ``shown_category`` is whatever the shopper is already looking at;
    it is left out, since offering someone the shelf they are standing in front
    of is not a suggestion. Everything else is ranked by how much it holds,
    because a fuller category is a better door into the shop than a thinner one.
    """
    answering = question_chips(reply)
    if not answering and reply and _asks_a_question(reply) and re.search(r"colou?r", _questions_in(reply), re.I):
        answering = question_chips(reply, await _stocked_colours(limit))
    if answering:
        return answering[:limit]

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
