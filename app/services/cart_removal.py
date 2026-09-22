"""Taking products out of the shopper's bag.

The bag lives in the shopper's browser, so the widget sends its lines with every
message and carries out the removal itself with the storefront's cart API. This
finds which of those lines the shopper meant - by name, narrowed by a colour or a
size - and says exactly which variant to take out and how many. It only ever
works from their own bag: a product that is not in it cannot be removed.
"""

from contextvars import ContextVar

from app.services import compare, shopper_words
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


async def cart_removals(items: list[dict]) -> dict:
    """Which lines of the bag to take out, and the instruction to take them out.

    Each item names a product in the bag, with a colour or size when the bag holds
    it in more than one, and how many to take out (all of that line if not said).
    """
    bound = _cart.get()
    if bound is None:
        return {"done": False, "reason": "cart_not_visible",
                "tell_customer": "I can't see your bag from here - you can remove it from the cart page."}
    lines, currency = bound
    if not lines:
        return {"done": False, "reason": "cart_empty", "in_cart": []}

    removals: dict[str, dict] = {}         # by variant id, so a line is never taken out twice
    needs_choice: list[dict] = []
    problems: list[dict] = []

    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("product") or item.get("title") or "").split())
        colour = str(item.get("color") or item.get("colour") or "").strip().lower()
        size = str(item.get("size") or "").strip().lower()

        if name.lower() in _EVERYTHING:
            matched = list(lines)
        else:
            matched = _named("" if name.lower() in _THIS else name, lines)
            if colour:
                matched = [l for l in matched if any(shopper_words.said_colour(o, colour) for o in _options(l))]
            if size:
                matched = [l for l in matched if any(shopper_words.said_size(o, size) for o in _options(l))]
            if not matched:
                problems.append({"asked_for": " ".join(p for p in (name, colour, size) if p) or "it",
                                 "reason": "not_in_cart",
                                 "in_cart": [_describe(l) for l in lines]})
                continue
            if len(matched) > 1:
                # The dress in Pink and in Blue are both in the bag: which one goes?
                needs_choice.append({"asked_for": name or "it", "missing": ["which_one"],
                                     "in_cart": [_describe(l) for l in matched]})
                continue

        for line in matched:
            variant_id = str(line.get("variant_id") or "").strip()
            if not variant_id:
                problems.append({"title": line.get("title"), "reason": "cannot_identify_line"})
                continue
            have = int(line.get("quantity") or 1)
            try:
                take = int(item.get("quantity") or have) if name.lower() not in _EVERYTHING else have
            except (TypeError, ValueError):
                take = have
            take = min(have, max(1, take))
            removals[variant_id] = {"line": line, "remove": take, "new_quantity": have - take}

    removed = []
    if removals:
        cards = await cart_cards([r["line"] for r in removals.values()], currency)
        for (variant_id, r), card in zip(removals.items(), cards):
            removed.append({**card, "variant_id": variant_id, "quantity": r["remove"],
                            "new_quantity": r["new_quantity"]})

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
