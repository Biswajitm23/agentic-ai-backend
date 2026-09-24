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
    ("who", re.compile(r"boy or (?:a )?girl|girl or (?:a )?boy|who is it for|who's it for|who are you shopping for", re.I)),
    ("age", re.compile(r"how old|what age|\bage\b", re.I)),
    ("budget", re.compile(r"budget|how much (?:would|do|can) you|spend", re.I)),
    ("colour", re.compile(r"colou?r", re.I)),
]


_QUESTION_RE = re.compile(r"[^.!?\n]*\?")
# Asking without a question mark: "just let me know the size you'd like for
# each dress." had shelves under it, because only "?" counted as asking.
_ASK_WITHOUT_MARK_RE = re.compile(
    r"[^.!?\n]*\b(?:let me know|tell me|please (?:choose|pick|select)|"
    r"(?:pick|choose) (?:one|a size|a colou?r|the size|the colou?r))\b[^.!?\n]*"
    # "Which size would you like for the Catherine dress, and I'll add it."
    r"|(?:^|(?<=[.!?\n]))\s*(?:which|what)\s+(?:sizes?|colou?rs?|one|of)\b[^.!?\n]*",
    re.I,
)


def _asking_sentences(reply: str) -> list[str]:
    return _QUESTION_RE.findall(reply or "") or _ASK_WITHOUT_MARK_RE.findall(reply or "")


def _asks_a_question(reply: str) -> bool:
    return bool(_asking_sentences(reply))


def _questions_in(reply: str) -> str:
    """Only the sentences that ask something.

    The rest of a reply describes, and describing is not asking: "here is a look
    under your 20,000 budget - want me to add it to the bag?" asks about the bag,
    and matching the whole reply put budget chips under it.
    """
    return " ".join(_asking_sentences(reply))


CHOICE_LIMIT = 10     # a size run is longer than a row of topic chips


def choice_chips(needs_choice: list[dict], reply: str = "", then: str = "") -> list[dict]:
    """The exact options add_to_cart is waiting on, for the first product that needs one.

    Tapped, a chip says "5Y for the Catherine Gingham dress": the shopper's own
    choice, in the product's own spelling, so it goes in without asking again.
    The option the reply asked about comes first when it asked about one.

    then: what the shopper was on the way to - " and checkout" - added to every
    prompt, so the tapped answer carries it on.
    """
    chips = _choice_chips(needs_choice, reply)
    return [{**c, "prompt": c["prompt"] + then} for c in chips] if then else chips


def _choice_chips(needs_choice: list[dict], reply: str) -> list[dict]:
    asked = _questions_in(reply).lower()
    for entry in needs_choice or []:
        missing = entry.get("missing") or []
        if "product" in missing:
            return [{"label": t, "prompt": t, "kind": "product"}
                    for t in entry.get("which_product") or []][:CHOICE_LIMIT]
        if "which_one" in missing:
            # Two lines of the bag answer to the name: which one is meant?
            verb = "Remove the" if entry.get("doing") == "remove" else "The"
            chips = []
            for line in entry.get("in_cart") or []:
                title, option = line.get("title") or "", line.get("option") or ""
                named = f"{title} ({option})" if option and option not in title else title
                chips.append({"label": option or title, "prompt": f"{verb} {named}", "kind": "cart_line"})
            return chips[:CHOICE_LIMIT]
        if "similar_in_bag" in missing:
            # The same kind of piece is already in the bag: replace it, keep both,
            # or leave this one out.
            new = entry.get("title") or "this one"
            there = entry.get("in_bag") or []
            old = there[0].get("title") if len(there) == 1 else "one in my bag"
            return [
                {"label": "Replace it", "prompt": f"Replace the {old} with the {new}", "kind": "similar"},
                {"label": "Keep both", "prompt": f"Keep both - add the {new} too", "kind": "similar"},
                {"label": "Don't add this one", "prompt": f"Don't add the {new}", "kind": "similar"},
            ]
        if "quantity" in missing:
            # "Change the quantity" with no number: the counts around what is there now.
            now = int(entry.get("quantity_now") or 1)
            title, option = entry.get("title") or "", entry.get("option") or ""
            named = f"{title} ({option})" if option and option not in title else title
            counts = [n for n in range(1, max(now + 3, 5)) if n != now][:6]
            return [{"label": str(n), "prompt": f"Make it {n} of the {named}", "kind": "quantity"}
                    for n in counts]
        kinds = [k for k in ("color", "size") if k in missing]
        if not kinds:
            continue
        kind = kinds[0]
        if len(kinds) == 2 and "size" in asked and not re.search(r"colou?r", asked):
            kind = "size"
        values = entry.get("available_colors" if kind == "color" else "available_sizes") or []
        # Changing a line: never offer back the size or colour it already is.
        values = [v for v in values if v != (entry.get("now") or {}).get(kind)]
        title = entry.get("title") or ""
        return [{"label": v, "prompt": f"{v} for the {title}" if title else v,
                 "kind": "colour" if kind == "color" else "size"} for v in values][:CHOICE_LIMIT]
    return []


_SIZE_WORD_RE = re.compile(r"\bsizes?\b", re.I)
_COLOUR_WORD_RE = re.compile(r"\bcolou?rs?\b", re.I)


def collection_chips(collections: list[dict]) -> list[dict]:
    """Every collection as a chip - the answer to "show me all your collections".

    All of them, not a handful: the shopper asked to see what there is.
    """
    return [{"label": c["title"], "prompt": f"What is in {c['title']}?", "kind": "collection",
             "id": c.get("handle")} for c in collections if c.get("title")]


def category_chips(categories: list[dict]) -> list[dict]:
    """Every product category as a chip - the answer to "what categories do you have".

    All of them, biggest first. Tapping one asks for it by name, which
    get_products_by_category answers.
    """
    return [{"label": f'{c["name"]} ({c["product_count"]})', "prompt": f'Show me {c["name"]}'}
            for c in categories if c.get("name")]


def _title_forms(title: str) -> list[str]:
    """How a reply shortens a title, longest first: "George Check Long Sleeve Shirt
    in Blue (12mths-10yrs)" is written "the George Check Long Sleeve Shirt"."""
    bare = re.sub(r"\s*\([^)]*\)", "", title).strip()
    plain = re.sub(r"\s+in\s+[^()]*$", "", bare).strip()
    return [f for f in dict.fromkeys((title, bare, plain)) if len(f) >= 8]


def _named_product(text: str, tree: dict) -> str | None:
    """The one product a piece of text names, or None for none or several."""
    lowered = (text or "").lower().replace("’", "'")
    found: dict[str, int] = {}
    for title, product_id in tree["ids"].items():
        form = next((f for f in _title_forms(title) if f in lowered), None)
        if form:
            found[product_id] = max(found.get(product_id, 0), len(form))
    if not found:
        return None
    longest = max(found.values())
    leaders = [pid for pid, length in found.items() if length == longest]
    return leaders[0] if len(leaders) == 1 else None


def _all_named(text: str, tree: dict) -> list[str]:
    """Every product a piece of text names, in the order it names them."""
    lowered = (text or "").lower().replace("’", "'")
    hits = []
    for title, product_id in tree["ids"].items():
        form = next((f for f in _title_forms(title) if f in lowered), None)
        if form:
            hits.append((lowered.find(form), len(form), product_id))
    named, taken_until = [], -1
    for start, length, product_id in sorted(hits, key=lambda h: (h[0], -h[1])):
        if start >= taken_until and product_id not in named:
            named.append(product_id)
            taken_until = start + length
    return named


async def either_chips(reply: str) -> list[dict]:
    """"The Brown Striped Belt or the Cream Boy's Belt?" - the products it offers, as chips."""
    asked = last_question(reply)
    if not re.search(r"\bor\b", asked, re.I):
        return []
    tree = await shopify_storefront.collection_tree()
    named = _all_named(asked, tree)
    if len(named) < 2:
        return []
    return [{"label": tree["titles"][pid], "prompt": tree["titles"][pid], "kind": "product"}
            for pid in named][:CHOICE_LIMIT]


async def asked_option_chips(reply: str, then: str = "", fallback_title: str | None = None) -> list[dict]:
    """A size or colour question the agent asked itself, answered with that product's options.

    "What size for the Cream Boy's Belt?" came with Explore Dresses under it: the
    agent asked on its own rather than through add_to_cart, so no options came
    back with the question. The product must be named - in the question, or as
    the only product in the reply - and must actually offer more than one of
    what was asked; nothing is guessed.
    """
    asked = _questions_in(reply).replace("’", "'")
    size_at = _SIZE_WORD_RE.search(asked)
    colour_at = _COLOUR_WORD_RE.search(asked)
    if not (size_at or colour_at):
        return []
    tree = await shopify_storefront.collection_tree()
    # Named in the question - or, when the question only says "its size", the
    # one product the rest of the reply is about - or, asked "for each", the
    # first of the products it names.
    product_id = _named_product(asked, tree) or _named_product(reply, tree)
    if product_id is None and re.search(r"\b(?:each|first|all)\b", asked, re.I):
        product_id = next(iter(_all_named(reply, tree)), None)
    # "the Catherine gingham dress" is no title at all: the caller matched the
    # shortened name against the products this chat has shown.
    if product_id is None and fallback_title:
        product_id = tree["ids"].get(fallback_title.strip().lower())
    if product_id is None:
        return []
    title, handle = tree["titles"][product_id], tree["handles"][product_id]

    from app.services import outfit

    product = next(iter(await outfit._active_products([handle])), None)
    if product is None:
        return []
    options = outfit._options_of(product)
    # Whichever the question asks about first.
    by_size = bool(size_at) and (not colour_at or size_at.start() < colour_at.start())
    values = options.get("Size") if by_size else (options.get("Color") or options.get("Colour"))
    if not values or len(values) < 2:
        return []
    kind = "size" if by_size else "colour"
    return [{"label": v, "prompt": f"{v} for the {title}{then}", "kind": kind} for v in values][:CHOICE_LIMIT]


YES_NO_CHIPS = [
    {"label": "Yes, please", "prompt": "Yes, please", "kind": "yes"},
    {"label": "No, thanks", "prompt": "No, thanks", "kind": "no"},
]
_YES_NO_RE = re.compile(
    r"^\s*(?:do|does|did|would|will|shall|should|can|could|may|is|are|was|were|want|wanna|"
    r"have|has|ready|happy|fancy|need|ok|okay)\b"
    r"|\b(?:want me to|shall i|should i|like me to|do you want|ok to|okay to|happy for me to)\b",
    re.I,
)
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|ya|sure|ok|okay|please|go ahead|do it|sounds good|perfect|"
    r"great|correct|that'?s? (?:fine|right|good|great))\b",
    re.I,
)
_CHOICE_LIST_RE = re.compile(r"^\s*\d+[.)]\s")


def last_question(reply: str) -> str:
    """The last thing the reply asked, or ""."""
    asked = _asking_sentences(reply)
    return asked[-1].strip() if asked else ""


_WH_RE = re.compile(r"^\s*(?:which|what|where|when|who|whom|whose|why|how)\b", re.I)


def _asking_part(question: str) -> str:
    """The part of a question that asks: "The first size is 12M; want me to add it?"
    asks "want me to add it", and "Just to be sure, which one?" asks "which one"."""
    tail = re.split(r"[;:–—]|\s-\s", question or "")[-1].strip()
    lead = re.match(r"^([^,]{0,40}),\s*(.+)$", tail)
    if lead and (_WH_RE.match(lead.group(2)) or _YES_NO_RE.match(lead.group(2))):
        tail = lead.group(2)
    return tail


def is_yes_no(question: str) -> bool:
    """A question answered yes or no - "want me to add it?", not "pink or blue?", and
    never one that starts "which" or "what": "Which of the dresses would you like
    me to add?" was answered with a Yes chip."""
    tail = _asking_part(question)
    if _WH_RE.match(tail):
        return False
    return bool(_YES_NO_RE.search(tail)) and not re.search(r"\bor\b", tail, re.I)


# "Which of the dresses?" asks for products; "which size", "which occasion" or
# "which order" do not, and have answers of their own.
_WHICH_PRODUCT_RE = re.compile(r"^\s*which\b", re.I)
_NOT_A_PRODUCT_RE = re.compile(
    r"\b(?:sizes?|colou?rs?|occasion|age|budget|day|date|time|email|order|address|reason|way)\b", re.I)


def asks_which_product(reply: str) -> bool:
    """Whether the reply's question asks them to pick among products."""
    asked = _asking_part(last_question(reply))
    return bool(_WHICH_PRODUCT_RE.match(asked)) and not _NOT_A_PRODUCT_RE.search(asked)


def product_chips(products: list[dict], then: str = "") -> list[dict]:
    """The products to pick from, and all of them at once when there are several."""
    titles = list(dict.fromkeys(p["title"] for p in products if p.get("title")))
    chips = [{"label": "All of them", "prompt": f"All of them{then}", "kind": "product"}] if len(titles) > 1 else []
    chips += [{"label": t, "prompt": f"{t}{then}", "kind": "product"} for t in titles]
    return chips[:CHOICE_LIMIT]


# Words that make an "option" a phrase of the question itself rather than an
# answer: "would you like to see more or shall I show you shoes?" offers none.
_QUESTION_WORDS = {"i", "you", "me", "shall", "should", "would", "could", "like", "want", "show", "let"}
MAX_OPTION_WORDS = 5


def options_in_question(reply: str) -> list[dict]:
    """The options an either/or question offers, as chips.

    "Did you mean all of the dresses, or only some of them?" - "All of the dresses"
    and "Only some of them". "Which size - S / 60cm, L / 70cm or XL / 80cm?" -
    each size. Each option is about as long as the last one, so the first is cut
    down to that length from the words before it. Nothing is offered when the
    options read as parts of a sentence rather than answers.
    """
    asked = last_question(reply).rstrip("?").strip()
    head, sep, last = asked.rpartition(" or ")
    if not sep:
        return []
    last = last.strip(" ,.")
    width = len(last.split())
    options = [last]
    for part in reversed([p.strip() for p in re.split(r",\s*", head.rstrip(", ")) if p.strip()]):
        words = part.split()
        if len(words) <= max(width, 3) + 1:
            options.insert(0, part)
            continue
        options.insert(0, " ".join(words[-width:]))
        break
    if not 2 <= len(options) <= 6:
        return []
    for option in options:
        words = [w.lower().strip(".,") for w in option.split()]
        if not words or len(words) > MAX_OPTION_WORDS or _QUESTION_WORDS & set(words):
            return []
    return [{"label": o[0].upper() + o[1:], "prompt": o[0].upper() + o[1:], "kind": "option"} for o in options]


def is_affirmative(message: str) -> bool:
    """A short yes - "yes please", "sure", "go ahead"."""
    return bool(_AFFIRMATIVE_RE.search(message or "")) and len((message or "").split()) <= 8


def _ends_in_choices(reply: str) -> bool:
    """The reply ends with "1." "2." choices, which the storefront draws as buttons itself."""
    lines = [line for line in (reply or "").splitlines() if line.strip()]
    return bool(lines) and bool(_CHOICE_LIST_RE.match(lines[-1]))


def answer_chips(reply: str) -> list[dict]:
    """For a question nothing more specific answers: yes and no, or nothing at all."""
    if _ends_in_choices(reply):
        return []
    return [dict(c) for c in YES_NO_CHIPS] if is_yes_no(last_question(reply)) else []


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
    if lowered.endswith(("ss", "x", "ch", "sh")):
        return f"{name}es"
    # Already plural - "Dresses", "T-Shirts". Adding to it gave "Explore Dresseses".
    if lowered.endswith("s"):
        return name
    return f"{name}s"


def _category_chip(entry: dict) -> dict:
    name = plural(entry["name"])
    return {"label": f"Explore {name}", "prompt": f"Show me {name.lower()}",
            "kind": "category", "id": entry.get("id")}


def _collection_chip(entry: dict) -> dict:
    title = entry.get("name") or entry.get("title") or ""
    return {"label": f"Shop {title}", "prompt": f"What is in {title}?",
            "kind": "collection", "id": entry.get("handle") or entry.get("id")}


# ── Where the shopper is ───────────────────────────────────────────────────
# The general shelves are the same whatever was just said - ask about Baby
# Accessories & Gifts and the row still offered Dresses and Shirts. So the row
# starts from where the shopper is: the shelves of the products this reply
# showed, then their neighbours under the same broad collection. A bib leads to
# Bibs, Teddy Bears, Socks, Belts and Sunglasses.

CONTEXT_LIMIT = 5


def _explore_chip(card: dict) -> dict:
    title = card.get("title") or card.get("name") or ""
    return {"label": f"Explore {title}", "prompt": f"What is in {title}?",
            "kind": "collection", "id": card.get("handle") or card.get("id")}


async def _context_chips(shown_products: list[dict], shown_category: dict | None) -> list[dict]:
    """Shelves around what the shopper is looking at, nearest first, or []."""
    tree = await shopify_storefront.collection_tree()
    handles: list[str] = []

    def add(handle: str | None) -> None:
        if handle and handle in tree["cards"] and handle not in handles:
            handles.append(handle)

    def neighbours(product_type: str) -> list[str]:
        return tree["children"].get(tree["parent"].get(product_type), [])

    def shelf(product_type: str) -> str | None:
        # Romper has no collection of its own, only Bodysuits & Rompers.
        return tree["home"].get(product_type) or tree["parent"].get(product_type)

    # 1. The products this reply showed: each one's own shelf, then its neighbours.
    types: list[str] = []
    for product in shown_products:
        found = (tree["type_of"].get(str(product.get("product_id") or ""))
                 or tree["type_of"].get((product.get("title") or "").strip().lower()))
        if found and found not in types:
            types.append(found)
    for t in types:
        add(shelf(t))
    for t in types:
        for handle in neighbours(t):
            add(handle)

    # 2. A collection or category the shopper named: what sits under it, or beside it.
    standing_in = None
    if shown_category:
        ident = shown_category.get("id")
        if shown_category.get("kind") == "collection" and ident:
            standing_in = ident
            if ident in tree["children"]:
                for handle in tree["children"][ident]:
                    add(handle)
            elif ident in tree["collection_type"]:
                for handle in neighbours(tree["collection_type"][ident]):
                    add(handle)
        elif shown_category.get("name"):
            named = shown_category["name"]
            standing_in = tree["home"].get(named)
            for handle in neighbours(named):
                add(handle)

    # The shelf they are already standing on is not a suggestion.
    return [_explore_chip(tree["cards"][h]) for h in handles if h != standing_in][:CONTEXT_LIMIT]


async def for_turn(shown_category: dict | None = None, limit: int = MAX_SUGGESTIONS,
                   reply: str = "", shown_products: list[dict] | None = None) -> list[dict]:
    """Chips to offer after a reply, most relevant first.

    1. A reply that asks the shopper something gets chips that answer it - and
       only those: yes and no when nothing more specific fits, else none.
    2. Otherwise the shelves around what they are looking at: the collections of
       the products this reply showed and their neighbours, or what sits under a
       collection they named. Up to five.
    3. Failing both, the general shelves.
    """
    answering = question_chips(reply)
    if not answering and reply and _asks_a_question(reply) and re.search(r"colou?r", _questions_in(reply), re.I):
        answering = question_chips(reply, await _stocked_colours(limit))
    if answering:
        return answering[:limit]

    # "Shall I show you our most popular pieces, or a category?" - the first
    # answer to that is the best-sellers chip, so it leads, and the categories
    # fill the rest in place of the collection.
    offers_best = bool(re.search(r"best.?sell|most popular|popular pieces|top (?:selection|pick)s?",
                                 _questions_in(reply), re.I))

    # A question is answered, never followed by shelves: "Explore Dresses" under
    # "what size for the George shirt?" reads as the choices on offer, and none
    # of them is one. Yes and no, or no chips at all.
    if not offers_best and _questions_in(reply).strip():
        try:
            either = await either_chips(reply)
        except Exception:  # noqa: BLE001 - chips are a nicety, never a reason to fail a reply
            logger.warning("Could not read the products a question offers", exc_info=True)
            either = []
        return either or options_in_question(reply) or answer_chips(reply)

    if not offers_best:
        try:
            context = await _context_chips(shown_products or [], shown_category)
        except Exception:  # noqa: BLE001 - chips are a nicety, never a reason to fail a reply
            logger.warning("Could not build context suggestions", exc_info=True)
            context = []
        if len(context) >= limit:
            return context[:CONTEXT_LIMIT]
        if context:
            # One shelf with no neighbours - Dresses - is topped up from the
            # general shelves rather than left as a single chip.
            labels = {c["label"] for c in context}
            filler = await _shelf_chips(shown_category, limit, offers_best=False)
            return (context + [c for c in filler if c["label"] not in labels])[:limit]

    return await _shelf_chips(shown_category, limit, offers_best)


async def _shelf_chips(shown_category: dict | None, limit: int, offers_best: bool) -> list[dict]:
    """The general shelves - the fullest categories, one collection, best sellers -
    for when the reply gives nothing to go on. The shelf the shopper is on is left
    out: offering it back is not a suggestion."""
    seen_id = (shown_category or {}).get("id")
    seen_name = ((shown_category or {}).get("name") or "").strip().lower()
    chips: list[dict] = [dict(BEST_SELLERS)] if offers_best else []

    try:
        listed = (await shopify_storefront.categories(CATEGORY_POOL))["categories"]
    except Exception:  # noqa: BLE001 - chips are a nicety, never a reason to fail a reply
        logger.warning("Could not build category suggestions", exc_info=True)
        listed = []

    for entry in listed:
        if len(chips) >= limit - (0 if offers_best else 1):
            break
        if entry.get("id") == seen_id or entry["name"].strip().lower() == seen_name:
            continue
        chip = _category_chip(entry)
        # "Dress" and "Dresses" are separate product types in the store but the
        # same shelf to a shopper - one "Explore Dresses" is enough.
        if any(c["label"] == chip["label"] for c in chips):
            continue
        chips.append(chip)

    # One collection alongside the plain categories: it is the merchandised door,
    # and it reads differently enough that the row does not look like one list.
    try:
        collections = (await shopify_storefront.collections(6))["collections"]
        for entry in collections:
            handle = entry.get("handle") or entry.get("id")
            if not offers_best and handle and handle != seen_id and len(chips) < limit:
                chips.append(_collection_chip(entry))
                break
    except Exception:  # noqa: BLE001
        logger.warning("Could not build a collection suggestion", exc_info=True)

    if not offers_best and len(chips) < limit:
        chips.append(dict(BEST_SELLERS))
    return chips[:limit]


async def for_welcome(limit: int = MAX_SUGGESTIONS) -> list[dict]:
    """Chips for the opening screen, where nothing has been shown yet."""
    return await for_turn(None, limit)
