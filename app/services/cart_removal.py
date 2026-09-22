"""Taking products out of the shopper's bag, or changing what is in it.

The bag lives in the shopper's browser, so the widget sends its lines with every
message and carries out the change itself with the storefront's cart API. This
finds which of those lines the shopper meant - by name, narrowed by a colour or a
size - and says exactly which variant to change and to what: fewer or more of
it, none of it, or the same product in another size or colour. It only ever
works from their own bag: a product that is not in it cannot be changed.
"""

from contextvars import ContextVar

from app.services import compare, outfit, shopper_words
from app.services.shopify_storefront import cart_cards

# The lines the widget sent this turn, or None when it sent no cart at all.
_cart: ContextVar[tuple[list[dict], str | None] | None] = ContextVar("shopper_cart", default=None)

# "Take everything out", "empty my bag".
_EVERYTHING = {"all", "everything", "every item", "all items", "all of it", "the lot", "whole bag",
               "whole cart", "all products"}
_THIS = {"", "it", "this", "that", "this one", "that one"}


def set_cart(lines: list[dict] | None, currency: str | None = None):
    """Bind the shopper's cart for this turn. Returns a token for ``reset``."""
    return _cart.set(None if lines is None else (list(lines), currency))


def reset(token) -> None:
    _cart.reset(token)


def _options(line: dict) -> list[str]:
    return [p.strip() for p in str(line.get("variant_title") or "").split("/") if p.strip()]


def _describe(line: dict) -> dict:
    return {"title": line.get("title"), "option": line.get("variant_title"),
            "quantity": line.get("quantity") or 1}


def _named(name: str, lines: list[dict]) -> list[dict]:
    """The lines a name points at: every word they used, else the closest few."""
    want = compare._stems(name)
    if not want:
        return list(lines)

    def stems(line: dict) -> set[str]:
        return compare._stems(f"{line.get('title') or ''} {line.get('handle') or ''} "
                              f"{line.get('variant_title') or ''}".replace("-", " "))

    holding = [line for line in lines if want <= stems(line)]
    if holding:
        return holding
    scored = [(len(want & stems(line)), line) for line in lines]
    best = max((s for s, _ in scored), default=0)
    # "the gingham dress" still finds the Catherine Gingham Embroidered Sleeveless
    # Trapeze Dress; one stray word in common is not enough.
    return [line for s, line in scored if best and s == best and s * 2 >= len(want)]


def _match(item: dict, lines: list[dict]) -> tuple[list[dict], str, bool]:
    """The lines an item points at, how to describe what was asked for, and whether it meant everything."""
    name = " ".join(str(item.get("product") or item.get("title") or "").split())
    colour = str(item.get("color") or item.get("colour") or "").strip().lower()
    size = str(item.get("size") or "").strip().lower()
    asked = " ".join(p for p in (name, colour, size) if p) or "it"
    if name.lower() in _EVERYTHING:
        return list(lines), asked, True
    matched = _named("" if name.lower() in _THIS else name, lines)
    if colour:
        matched = [l for l in matched if any(shopper_words.said_colour(o, colour) for o in _options(l))]
    if size:
        matched = [l for l in matched if any(shopper_words.said_size(o, size) for o in _options(l))]
    return matched, asked, False


def _not_in_cart(asked: str, lines: list[dict]) -> dict:
    return {"asked_for": asked, "reason": "not_in_cart", "in_cart": [_describe(l) for l in lines]}


def _which_one(asked: str, matched: list[dict], doing: str) -> dict:
    # The dress in Pink and in Blue are both in the bag: which one?
    return {"asked_for": asked, "missing": ["which_one"], "doing": doing,
            "in_cart": [_describe(l) for l in matched]}


def _bound() -> tuple[list[dict], str | None] | dict:
    bound = _cart.get()
    if bound is None:
        return {"done": False, "reason": "cart_not_visible",
                "tell_customer": "I can't see your bag from here - you can change it on the cart page."}
    lines, currency = bound
    if not lines:
        return {"done": False, "reason": "cart_empty", "in_cart": []}
    return lines, currency


def _unit(card: dict, have: int) -> float | None:
    """The bag gives a line's total; a card shows one unit's price."""
    return round(card["price"] / have, 2) if card.get("price") is not None and have else None


async def cart_removals(items: list[dict]) -> dict:
    """Which lines of the bag to take out, and the instruction to take them out.

    Each item names a product in the bag, with a colour or size when the bag holds
    it in more than one, and how many to take out (all of that line if not said).
    """
    bound = _bound()
    if isinstance(bound, dict):
        return bound
    lines, currency = bound

    removals: dict[str, dict] = {}         # by variant id, so a line is never taken out twice
    needs_choice: list[dict] = []
    problems: list[dict] = []

    for item in items or []:
        if not isinstance(item, dict):
            continue
        matched, asked, everything = _match(item, lines)
        if not matched:
            problems.append(_not_in_cart(asked, lines))
            continue
        if len(matched) > 1 and not everything:
            needs_choice.append(_which_one(asked, matched, "remove"))
            continue

        for line in matched:
            variant_id = str(line.get("variant_id") or "").strip()
            if not variant_id:
                problems.append({"title": line.get("title"), "reason": "cannot_identify_line"})
                continue
            have = int(line.get("quantity") or 1)
            try:
                take = int(item.get("quantity") or have) if not everything else have
            except (TypeError, ValueError):
                take = have
            take = min(have, max(1, take))
            removals[variant_id] = {"line": line, "remove": take, "new_quantity": have - take}

    removed = []
    if removals:
        cards = await cart_cards([r["line"] for r in removals.values()], currency)
        for (variant_id, r), card in zip(removals.items(), cards):
            unit = _unit(card, r["remove"] + r["new_quantity"])
            removed.append({**card, "variant_id": variant_id, "quantity": r["remove"],
                            "new_quantity": r["new_quantity"], "price": unit,
                            "line_total": round(unit * r["remove"], 2) if unit is not None else None})

    # A question still open means nothing is taken out yet: the answer decides.
    done = bool(removed) and not needs_choice
    result: dict = {
        "done": done,
        "currency": currency,
        ("removed" if done else "not_removed_yet"): removed,
        "needs_choice": needs_choice,
        "problems": problems,
    }
    if done:
        result["action"] = {
            "type": "remove_from_cart",
            "items": [{"variant_id": r["variant_id"], "title": r.get("title"), "option": r.get("option"),
                       "quantity": r["quantity"], "new_quantity": r["new_quantity"]} for r in removed],
        }
    return result


def _new_quantity(item: dict, have: int) -> int | None:
    """How many they want afterwards: a total ("make it 1"), a step ("one less"), or None."""
    for key in ("quantity", "change_by"):
        raw = item.get(key)
        if raw in (None, ""):
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        return have + number if key == "change_by" else number
    return None


async def cart_edits(items: list[dict]) -> dict:
    """What to change about lines already in the bag, and the instruction to change them.

    A new quantity, or the same product in another size or colour. The line is
    found the way a removal finds it. A new size or colour must be one the
    shopper said, exactly as when adding; the option they are not changing stays
    what they already chose.
    """
    bound = _bound()
    if isinstance(bound, dict):
        return bound
    lines, currency = bound

    edits: dict[str, dict] = {}            # by variant id: the line and what it becomes
    additions: list[dict] = []             # a new size or colour goes in as its own line
    needs_choice: list[dict] = []
    problems: list[dict] = []

    for item in items or []:
        if not isinstance(item, dict):
            continue
        matched, asked, _ = _match(item, lines)
        if not matched:
            problems.append(_not_in_cart(asked, lines))
            continue
        if len(matched) > 1:
            needs_choice.append(_which_one(asked, matched, "edit"))
            continue
        line = matched[0]
        variant_id = str(line.get("variant_id") or "").strip()
        if not variant_id:
            problems.append({"title": line.get("title"), "reason": "cannot_identify_line"})
            continue

        have = int(line.get("quantity") or 1)
        what = str(item.get("what") or "").strip().lower()
        new_colour = str(item.get("new_color") or item.get("new_colour") or "").strip()
        new_size = str(item.get("new_size") or "").strip()
        want = _new_quantity(item, have)
        described = {"title": line.get("title"), "option": line.get("variant_title"), "doing": "edit"}

        product = None
        if new_colour or new_size or what in ("size", "color", "colour"):
            product = next(iter(await outfit._active_products([line["handle"]])), None) if line.get("handle") else None
            if product is None:
                problems.append({**described, "reason": "not_found_or_not_for_sale"})
                continue
        if product is not None:
            offered = outfit._options_of(product)
            colours = offered.get("Color") or offered.get("Colour") or []
            sizes = offered.get("Size") or []
            # "Change the size" with no size named: ask which, from what it comes in.
            missing = [k for k, asked_for, have_new, offered_k in (
                ("size", what == "size", new_size, sizes), ("color", what in ("color", "colour"), new_colour, colours))
                if asked_for and not have_new and len(offered_k) > 1]
            # Only the option being changed must be the shopper's own word.
            words = shopper_words.current()
            unconfirmed = {}
            if new_colour and words is not None and not shopper_words.said_colour(new_colour, words):
                unconfirmed["color"] = new_colour
            if new_size and words is not None and not shopper_words.said_size(new_size, words):
                unconfirmed["size"] = new_size
            current = next((v for v in product["variants"]["nodes"]
                            if str(v.get("legacyResourceId")) == variant_id), None)
            if missing or unconfirmed:
                # What it is now, so it is not offered back as the change.
                now = {"color": outfit._colour_of(current), "size": outfit._option_value(current, "Size")}                     if current else {}
                needs_choice.append({**outfit._choice_needed(product["title"], missing + list(unconfirmed),
                                                             unconfirmed, colours, sizes),
                                     "doing": "edit", "now": now})
                continue
            variant = outfit._match_variant(
                product,
                new_colour or (outfit._colour_of(current) if current else None),
                new_size or (outfit._option_value(current, "Size") if current else None),
            )
            if variant is None:
                problems.append({**described, "reason": "no_variant_for_that_choice",
                                 "available_colors": colours, "available_sizes": sizes})
                continue
            if str(variant.get("legacyResourceId")) != variant_id:
                if not variant["availableForSale"]:
                    problems.append({**described, "wanted": variant["title"], "reason": "out_of_stock"})
                    continue
                # Another size or colour: this line goes, the new one comes in.
                edits[variant_id] = {"line": line, "new_quantity": 0, "have": have, "change": "removed"}
                quantity = min(outfit.MAX_CART_QUANTITY, max(1, want if want else have))
                additions.append({**outfit._cart_line(product, variant, quantity), "currency": currency})
                continue

        if want is None:
            # "Change the quantity" with no number: how many should there be?
            needs_choice.append({**described, "missing": ["quantity"], "quantity_now": have})
            continue
        want = max(0, min(outfit.MAX_CART_QUANTITY, want))
        if want == have:
            problems.append({**described, "reason": "no_change", "quantity_now": have})
            continue
        edits[variant_id] = {"line": line, "new_quantity": want, "have": have,
                             "change": "edited" if want else "removed"}

    changed = []
    if edits:
        cards = await cart_cards([e["line"] for e in edits.values()], currency)
        for (variant_id, e), card in zip(edits.items(), cards):
            unit = _unit(card, e["have"])
            # An edited card shows what the line becomes; a removed one what came out.
            quantity = e["new_quantity"] if e["change"] == "edited" else e["have"]
            changed.append({**card, "variant_id": variant_id, "price": unit, "change": e["change"],
                            "quantity": quantity, "quantity_before": e["have"],
                            "new_quantity": e["new_quantity"],
                            "line_total": round(unit * quantity, 2) if unit is not None else None})
    for line in additions:
        changed.append({**line, "price": line["unit_price"], "change": "added"})

    done = bool(changed) and not needs_choice
    result: dict = {
        "done": done,
        "currency": currency,
        ("changed" if done else "not_changed_yet"): changed,
        "needs_choice": needs_choice,
        "problems": problems,
    }
    if done:
        action: dict = {
            "type": "edit_cart",
            "items": [{"variant_id": c["variant_id"], "title": c.get("title"), "option": c.get("option"),
                       "color": c.get("color"), "size": c.get("size"),
                       "quantity_before": c["quantity_before"], "new_quantity": c["new_quantity"]}
                      for c in changed if c["change"] != "added"],
        }
        adding = [{"variant_id": c["variant_id"], "quantity": c["quantity"]} for c in changed if c["change"] == "added"]
        if adding:
            action["add_items"] = adding
        result["action"] = action
    return result
