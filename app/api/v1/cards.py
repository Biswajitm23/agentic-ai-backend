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
    "browse_category": "products",
    "get_products_by_category": "products",
    "get_best_sellers": "products",
    "browse_catalogue": "products",
    "suggest_pieces": "products",
    "compare_products": "products",
    "recommend_for_me": "products",
    # What went into or came out of the bag - and only that - when a turn changed it.
    "add_to_cart": "products",
    "remove_from_cart": "products",
    "edit_cart": "products",
    "build_outfit": "outfit",
    "get_my_order_history": "orders",
    "check_order_status": "orders",
    # Not a card - a set of buttons. Same idea though: the shopper should be
    # tapping a choice, not reading the agent recite seven of them.
    "request_order_change": "choices",
}
MAX_CARDS = 12

# Tools whose product list IS the answer, not a shortlist the agent then talks
# about. A category browse is the shopper's own request drawn back at them, so
# it is sent whole - trimming it to the few products the reply names would empty
# a grid the shopper explicitly asked to see.
WHOLE_RESULT_TOOLS = {"browse_category"}

# Tools whose every product is sent, past MAX_CARDS: a tapped product category
# is the shopper asking to see all of it, and thirteen dresses is thirteen cards.
ALL_RESULT_TOOLS = {"get_products_by_category"}

# Tools that change the bag: where their result keeps what changed, what kind
# of change it is, and how the cards are headed.
BAG_TOOLS = {
    "add_to_cart": ("added", "added", "Added to your bag"),
    "remove_from_cart": ("removed", "removed", "Removed from your bag"),
    "edit_cart": ("changed", "edited", "Your bag was updated"),
}

# Tools whose products are never trimmed to the wording. A comparison is every
# product in it, whichever of them the reply happens to name in full.
FIXED_RESULT_TOOLS = {"compare_products", *BAG_TOOLS}


_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say nothing about which product this is. Held as stems, and matched
# after stemming, so "boys" and "boy" are both caught - previously only the
# plural was listed, and the singular in "is it for a boy or a girl?" picked out
# the Cream Boy's Belt and drew it under a question that named no product.
#
# The second group describes the shopper, not the garment. Those words turn up
# in every clarifying question we ask, so a title carrying one must not be
# recognised by it: "Leather T Bar Baby Shoes" is still found by "t bar".
_NOISE = {
    "the", "and", "for", "with", "in", "of", "a", "an",
    # Plain English that a title happens to hold: "made for little ones" drew
    # the Cashmere All In One under a reply that named no product at all.
    "one", "all", "two", "set", "pair", "new",
    "kid", "girl", "boy", "child", "children", "baby", "toddler",
    "year", "old", "size", "colour", "color", "man", "men", "woman", "women",
}
# Used only when a title has no word of its own to be recognised by.
_MENTION_RATIO = 0.5
# Kinds of piece, as stems. Over a whole shelf they describe it - "from shirts
# and jumpers to trousers" - rather than pick a product out, even when only one
# title there carries the word: read as names, they cut nineteen boys' pieces
# down to the four that happened to say "jacket", "trousers" or "booties".
# "dres" is not a typo: _stem() takes the last s off "dress" too.
_KINDS = {
    "dres", "shirt", "top", "blouse", "jumper", "sweater", "cardigan", "knitwear",
    "jacket", "coat", "trouser", "short", "skirt", "legging", "romper", "bodysuit",
    "sleepsuit", "pyjama", "nightwear", "shoe", "boot", "bootie", "sandal", "plimsoll",
    "hat", "bonnet", "cap", "hairband", "headband", "bow", "belt", "sock", "bib",
    "blanket", "sunglasse", "toy", "teddy", "bear", "accessorie", "gift", "piece",
}


def _money_forms(total) -> list[str]:
    """How a reply may write a total: "36300.00", "36,300.00", "36300"."""
    try:
        value = float(total)
    except (TypeError, ValueError):
        return []
    forms = [f"{value:.2f}", f"{value:,.2f}"]
    if value == int(value):
        forms += [f"{int(value):,}", str(int(value))]
    return forms


def _stem(word: str) -> str:
    """Fold simple plurals so "Mary Janes" still finds "Mary Jane"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words(text: str) -> set[str]:
    stems = (_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) > 2)
    return {s for s in stems if s not in _NOISE}


def _tokens(text: str) -> list[str]:
    """The reply's words in order, as _words() reads them."""
    stems = (_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) > 2)
    return [s for s in stems if s not in _NOISE]


def _names_other_piece(words: set[str], own: set[str], tokens: list[str]) -> bool:
    """Every place the reply uses this title's words, it goes on to name a
    different kind of piece.

    "Hand Smocked Peter Pan Collar Short Sleeve Dress in Burgundy" is not the
    "... Short Sleeve Romper": the words it shares run into "dress", which the
    romper's title does not have. With the dress missing from the tool result
    those shared words were the romper's alone, and drew it under a reply about
    two dresses. Only a title that says what it is can be contradicted this way.
    """
    if not words & _KINDS:
        return False
    hits = [i for i, t in enumerate(tokens) if t in own]
    if not hits:
        return False
    for i in hits:
        end = i
        while end + 1 < len(tokens) and tokens[end + 1] in words:
            end += 1
        after = tokens[end + 1] if end + 1 < len(tokens) else None
        if not (after in _KINDS and after not in words):
            return False
    return True


def keep_mentioned(items: list[dict], reply: str, ignore: set[str] | None = None) -> list[dict]:
    """The products the reply actually talks about.

    Prose shortens titles - "Catherine Gingham Embroidered Sleeveless Trapeze
    Dress" becomes "the Catherine Gingham dress", "Leather Mary Jane Shoes"
    becomes "the Mary Janes" - so a product counts as mentioned when the reply
    uses a word that belongs to it alone.

    Matching on any shared word would be wrong in the other direction: with
    "Leather T Bar Baby Shoes" in the reply, "leather" and "shoes" must not drag
    the Mary Janes in beside it.

    ignore: stems in the reply that must not count as naming anything.
    """
    said = _words(reply) - (ignore or set())
    tokens = _tokens(reply)
    title_words = [(item, _words(item.get("title") or "")) for item in items]

    frequency: dict[str, int] = {}
    for _, words in title_words:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1

    kept, by_share = [], []
    for item, words in title_words:
        if not words:
            continue
        distinctive = {w for w in words if frequency.get(w, 1) == 1}
        if distinctive:
            if distinctive & said and not _names_other_piece(words, distinctive & said, tokens):
                kept.append(item)
        else:
            # Nothing sets this title apart, so fall back to how much of it appears -
            # counting what names this piece, not the kind of piece it is: "belt" in
            # "here's our brown belt" was half of "Cream Boy's Belt" and drew it too.
            core = (words - _KINDS) or words
            if len(core & said) / len(core) >= _MENTION_RATIO:
                kept.append(item)
                by_share.append(item)
    # A title that is only part of another one kept - "Catherine ... Dress" inside
    # "Catherine ... Dress in Pink" - was matched by that one's words: one dress
    # named, two cards drawn. It goes.
    words_of = {id(item): words for item, words in title_words}
    return [item for item in kept
            if not (item in by_share and any(words_of[id(item)] < words_of[id(other)]
                                             for other in kept if other is not item))]


def keep_orders_mentioned(orders: list[dict], reply: str) -> list[dict]:
    """The orders the reply actually talks about.

    "What was my last order?" is answered about one order, but the tool hands
    back the last five, and drawing all of them put four the shopper did not ask
    about under an answer about one.

    An order number is only counted when it is written as one - "#1033", or
    "order 1033". A bare 1033 is ignored on purpose: replies are full of prices
    and totals, and "1033.00" should not pull up order 1033.
    """
    kept = []
    for order in orders:
        bare = (order.get("order_number") or "").lstrip("#").strip()
        if not bare:
            continue
        num = re.escape(bare)
        if re.search(rf"#\s*{num}\b", reply) or re.search(rf"\border\s+#?{num}\b", reply, re.I):
            kept.append(order)
    return kept


_CHOICE_LINE_RE = re.compile(r"^\s*\d+[.)]\s")


def _without_choices(reply: str) -> str:
    """The reply minus the numbered choices the storefront turns into buttons.

    Those lines are the agent's own questions - "everyday, party, school" - and
    matching products against them drags in whatever happens to share a word with
    a menu option, like a Party Dress under a question about the occasion.
    """
    lines = reply.splitlines()
    while lines and (not lines[-1].strip() or _CHOICE_LINE_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines)


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

    if tool_name in BAG_TOOLS:
        key, change, heading = BAG_TOOLS[tool_name]
        # An add that made room shows what came out beside what went in.
        items = [*(data.get("replaced") or []), *(data.get(key) or [])]
        if not items:
            return None                     # nothing changed: a question, or not in the bag
        return {
            "items": [card(i) | {"quantity": i.get("quantity"), "change": i.get("change") or change,
                                 "color": i.get("color"), "size": i.get("size"),
                                 "options": i.get("options") or {}}
                      | {k: i[k] for k in ("new_quantity", "quantity_before") if k in i}
                      for i in items],
            "currency": currency,
            "heading": heading,
            "layout": change,
        }

    if tool_name == "compare_products":
        items = data.get("products") or []
        if len(items) < 2:
            return None                     # one product is not a comparison
        # Ordinary product cards, so they render as they are today, carrying the
        # spec rows and highlights for a widget that wants to line them up.
        return {
            "items": [card(i) | {"specs": i.get("specs") or [], "highlights": i.get("highlights") or []}
                      for i in items],
            "currency": currency,
            "heading": data.get("heading"),
            "layout": "comparison",
            # Where they differ, one row per attribute, values in card order -
            # ready to lay out as a table beside the cards.
            "difference": data.get("difference") or [],
        }

    items = data.get("products") or []
    if not items:
        return None
    # Every product stays for now. finalise() matches these against the reply and
    # as_dict() takes the cap: trimming here first meant a catalogue of fifty was
    # cut to twelve before anyone asked which ones the agent had named, so a
    # product mentioned from further down the list had no card to attach to.
    result = {"items": [card(i) for i in items], "currency": currency}
    # A title for the row, when the tool has one - "Picked for you: Dresses and
    # Cardigans" above a set of recommendations.
    if data.get("heading"):
        result["heading"] = data["heading"]
    if tool_name in ALL_RESULT_TOOLS and data.get("category"):
        result["group"] = data["category"]
    return result


class CardCollector:
    """Gathers cards across a turn so they can be sent mid-stream and at the end.

    A turn may call several tools; the last product result is the one the reply
    is actually about, so later cards replace earlier ones of the same kind.
    """

    def __init__(self) -> None:
        self.products: dict | None = None
        self.products_whole = False
        self.products_fixed = False
        self.products_all = False
        # Instructions for the widget - add these variants, open checkout - in
        # the order the agent issued them.
        self.actions: list[dict] = []
        # The last add_to_cart put nothing in - it is asking for a size, colour or
        # which product - so the widget must not act on the bag this turn.
        self.cart_waiting = False
        # ...and what it is waiting for: the options still to choose, per product.
        self.cart_choice: list[dict] = []
        # What went into or came out of the bag this turn, one card set per call.
        # When there is any, it is the only products sent.
        self.bag_cards: list[dict] = []
        # Every collection, when the shopper asked to see them all: sent as chips.
        self.collections_listed: list[dict] = []
        # The product categories shown, when the shopper asked to see them: sent as
        # chips, with how many there are in all so the rest can be offered.
        self.categories_listed: list[dict] = []
        self.categories_total = 0
        # What the shopper is looking at, so the follow-on chips can skip it.
        self.category: dict | None = None
        # Offered when the category asked for does not exist; drawn as tiles.
        self.categories: dict | None = None
        self.outfit: dict | None = None
        # Every look priced this turn. The agent often builds two - the swap and
        # the in-budget fallback - and the last one built is not always the one
        # the reply presents.
        self.outfits: list[dict] = []
        self.orders: dict | None = None
        self.choices: dict | None = None
        # Options the agent chose on a product for the shopper - pressed on its
        # card by the widget, never added.
        self.selection: dict | None = None
        # The buttons the agent chose for its own question, from real options.
        self.offered_choices: list[str] = []

    def take(self, tool_name: str, output: str | None) -> tuple[str, dict] | None:
        """Record a tool result. Returns (event_name, payload) when it had cards."""
        if tool_name == "offer_choices":
            try:
                self.offered_choices = json.loads(output or "{}").get("choices") or []
            except (TypeError, ValueError, AttributeError):
                self.offered_choices = []
            return None
        if tool_name == "select_options":
            try:
                result = json.loads(output or "{}")
            except (TypeError, ValueError):
                result = {}
            if result.get("found") and (result.get("selection") or {}).get("options"):
                self.selection = result["selection"]
            return None
        if output and '"action"' in output:
            try:
                action = json.loads(output).get("action")
            except (TypeError, ValueError, AttributeError):
                action = None
            if isinstance(action, dict) and action.get("type"):
                self.actions.append(action)
        if tool_name in BAG_TOOLS:
            try:
                result = json.loads(output or "{}")
                # add_to_cart puts in what is chosen and asks about the rest; the
                # others change nothing until every question is answered.
                self.cart_waiting = (bool(result.get("needs_choice")) if tool_name == "add_to_cart"
                                     else not result.get("done"))
                self.cart_choice = result.get("needs_choice") or []
            except (TypeError, ValueError, AttributeError):
                self.cart_waiting, self.cart_choice = True, []
        if tool_name == "list_product_categories" and output and '"listing": "categories"' in output:
            try:
                listing = json.loads(output)
                self.categories_listed = listing.get("categories") or []
                self.categories_total = int(listing.get("count") or len(self.categories_listed))
            except (TypeError, ValueError, AttributeError):
                self.categories_listed, self.categories_total = [], 0
            return None
        if tool_name == "get_products_by_category" and output and '"found": true' in output:
            # The list was only the way to this category; its buttons are not the answer.
            self.categories_listed = []
        if tool_name in ("list_collections", "browse_category") and output and '"listing": "collections"' in output:
            try:
                self.collections_listed = json.loads(output).get("collections") or []
            except (TypeError, ValueError, AttributeError):
                self.collections_listed = []
            return None
        if tool_name == "browse_category" and output:
            try:
                found = json.loads(output)
            except (TypeError, ValueError):
                found = {}
            self.category = found.get("category") if found.get("found") else None
            if not found.get("found"):
                offered = [c for c in (found.get("categories") or []) if c.get("name")]
                if offered:
                    self.categories = {"categories": offered[:MAX_CARDS]}
        cards = cards_from(tool_name, output)
        if cards is None:
            return None
        name = CARD_TOOLS[tool_name]
        if name == "products":
            # Set per result, so a later ordinary search still gets reconciled.
            self.products_whole = tool_name in WHOLE_RESULT_TOOLS
            self.products_fixed = tool_name in FIXED_RESULT_TOOLS
            self.products_all = tool_name in ALL_RESULT_TOOLS
        if name == "outfit":
            self.outfits.append(cards)
        if tool_name in BAG_TOOLS:
            self.bag_cards.append(cards)
        setattr(self, name, cards)
        return name, cards

    def settle_bag(self, actions: dict | None) -> None:
        """Keep the added and removed cards only for the changes really being made.

        The action decides - a question later in the same turn holds an add back -
        and a card saying "added" beside a bag that did not change would be a lie.
        """
        if not self.bag_cards or self.products is None:
            return
        going = {str(line.get("variant_id"))
                 for line in [*((actions or {}).get("items") or []), *((actions or {}).get("add_items") or [])]}
        kept = [i for i in self.products.get("items") or [] if str(i.get("variant_id")) in going]
        self.products = {**self.products, "items": kept} if kept else None

    def _presented_outfit(self, reply: str) -> None:
        """Send the look the reply presents, and what else it offered beside it.

        "Here's the look with Chelsea boots ... or the Plimsolls keep it in
        budget" is two builds, and the widget drew the Plimsolls look - the last
        one priced - under a reply about boots. The reply quotes the total of the
        look it presents first, so that one leads; failing a total, the one it
        names most of, the latest on a tie.
        """
        chosen = self.outfit
        if len(self.outfits) > 1:
            def placed(look: dict) -> int:
                spots = [reply.find(t) for t in _money_forms(look.get("total")) if t in reply]
                return min(spots) if spots else len(reply) + 1

            everyone = [i for look in self.outfits for i in look.get("items") or []]
            named = {id(i) for i in keep_mentioned(everyone, reply)}
            chosen = min(
                reversed(self.outfits),
                key=lambda look: (placed(look), -sum(id(i) in named for i in look.get("items") or [])),
            )
        if chosen is None:
            return
        # Pieces offered as the other choice: from a look not sent, or picked out
        # of the catalogue the agent browsed to find them.
        inside = {i.get("product_id") for i in chosen.get("items") or []}
        pool, pooled = [], set()
        for item in [*(i for look in self.outfits if look is not chosen for i in look.get("items") or []),
                     *((self.products or {}).get("items") or [])]:
            # Once each: a piece twice over has no word of its own to be found by.
            if item.get("product_id") not in pooled:
                pooled.add(item.get("product_id"))
                pool.append(item)
        alternatives, seen = [], set(inside)
        for item in keep_mentioned(pool, reply, ignore=_KINDS):
            if item.get("product_id") not in seen:
                seen.add(item.get("product_id"))
                alternatives.append(item)
        self.outfit = {**chosen, "alternatives": alternatives[:MAX_CARDS]} if alternatives else chosen

    def finalise(self, reply: str) -> None:
        """Reconcile the cards with the answer the shopper actually reads.

        A tool hands back everything it found - the whole catalogue, ten search
        results - and the agent then picks a few to talk about. Sending all of
        them would show five products under a list of three. So once the reply
        exists, keep only what it mentions.

        An outfit is exempt: it *is* the answer, priced and totalled, so it is
        sent whole, and the browse that fed it is dropped as noise.
        """
        # Changing the bag shows what went in or came out, and nothing else: not
        # the search that found it, not the rest of the bag.
        if self.bag_cards:
            items = [i for cards in self.bag_cards for i in cards["items"]]
            changes = {i.get("change") for i in items}
            single = len(changes) == 1
            self.products = {
                "items": items,
                "currency": self.bag_cards[-1].get("currency"),
                "heading": self.bag_cards[0]["heading"] if single else "Your bag was updated",
                "layout": next(iter(changes)) if single else "bag_update",
            }
            self.products_fixed = True
            return
        # An outfit or an order listing IS the answer, so any browse that fed it
        # is dropped as noise - once it has given up any alternative the reply
        # offered from it.
        if self.outfit is not None:
            self._presented_outfit(reply)
        if self.outfit is not None or self.orders is not None:
            self.products = None
        if self.orders is not None:
            listed = self.orders.get("orders") or []
            named = keep_orders_mentioned(listed, reply)
            # Nothing named is "here are your orders" - keep them all. One named
            # is "your last order was #1033", and the rest are not the answer.
            if named:
                self.orders = {**self.orders, "orders": named}
        if self.outfit is not None or self.orders is not None:
            return
        # Choices are never reconciled against the wording: the whole point is
        # that they do not depend on what the agent chose to say.
        if self.products is None or self.products_fixed or self.products_all:
            return
        items = self.products.get("items") or []
        kept = keep_mentioned(items, _without_choices(reply),
                              ignore=_KINDS if self.products_whole else None)

        if self.products_whole:
            # A bare category browse names nothing - "here is our Dress category,
            # 7 styles" - and the grid IS the answer, so it goes whole.
            #
            # But the shopper can ask for a slice of that category: "a white
            # dress for a 7 year old" still browses Dress, and the reply then
            # picks out the two that qualify. Sending the category anyway put
            # five dresses that are the wrong colour and the wrong size under an
            # answer that had already ruled them out. Once the reply names
            # products, those products are the answer.
            if kept:
                self.products = {**self.products, "items": kept}
            return
        # A reply that names nothing is an apology, a question, or a refusal - and
        # none of those should be sitting under a grid of products. Keeping the
        # whole list there was worse than showing none: it put girls' party
        # dresses under "I have nothing for a 9 year old boy".
        self.products = {**self.products, "items": kept} if kept else None

    def drop_empty_checkout(self) -> None:
        """Take out a checkout redirect when the bag is empty - unless this very turn
        put something in it. The agent said "your bag is empty" and still called
        checkout, which would have sent the shopper to an empty checkout page."""
        if any(a.get("type") == "add_to_cart" for a in self.actions):
            return
        self.actions = [a for a in self.actions
                        if not (a.get("type") == "redirect" and a.get("page") == "checkout")]

    def limit_products(self, count: int | None) -> None:
        """Hold the product row to what the shopper asked for - "2 jackets" is two
        cards, not the whole shelf. A comparison is left alone: it is exactly the
        products they named."""
        if not count or self.products is None or self.products_fixed:
            return
        items = self.products.get("items") or []
        if len(items) > count:
            self.products = {**self.products, "items": items[:count]}

    def shown_products(self) -> list[dict]:
        """Every product this reply put in front of the shopper - the grid and any
        outfit - so the follow-on chips can start from where they are."""
        return [*((self.products or {}).get("items") or []),
                *((self.outfit or {}).get("items") or [])]

    def as_dict(self) -> dict:
        """Whatever was collected, for the final payload."""
        out: dict = {}
        if self.products is not None:
            items = self.products.get("items") or []
            # A whole shelf or category goes whole: the reply says "30 pieces in
            # all", and the widget folds the rest under "Show N more" itself.
            capped = len(items) > MAX_CARDS and not (self.products_all or self.products_whole)
            out["products"] = {**self.products, "items": items[:MAX_CARDS]} if capped else self.products
        if self.outfit is not None:
            out["outfit"] = self.outfit
        if self.orders is not None:
            out["orders"] = self.orders
        if self.selection is not None:
            out["select"] = self.selection
        if self.choices is not None:
            out["choices"] = self.choices
        if self.categories is not None:
            out["categories"] = self.categories
        return out
