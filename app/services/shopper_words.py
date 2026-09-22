"""What the shopper has said in this conversation, and whether it chose an option.

Nothing goes in the bag in a size or colour the shopper never gave. The agent
is told to ask, but it also prices whole looks in sizes it picked itself, and a
"yes, add it" to that look is not the shopper choosing 35EU shoes. So the cart
checks every choice with more than one option against the shopper's own words:
a value they never said comes back as a question.

Bound per turn, like the shopper's identity. Unbound - a script, a test - means
there is no conversation to check against, and every choice stands.
"""

import re
from contextvars import ContextVar

# (everything they have said, what they said this turn)
_words: ContextVar[tuple[str, str] | None] = ContextVar("shopper_words", default=None)


def _plain(text: str) -> str:
    # Bracketed text is a product title's age range - "(12mths- 10yrs)" - carried
    # in by a tapped chip, never a size the shopper chose: it read as 12M, and a
    # dress went into the bag in a size nobody asked for.
    text = re.sub(r"\([^)]*\)", " ", text or "")
    return " ".join(text.lower().replace("’", "'").split())


def set_words(messages: list[str], this_turn: str = ""):
    """Bind the shopper's messages for this turn. Returns a token for ``reset``."""
    return _words.set((" ".join(_plain(m) for m in messages if m), _plain(this_turn)))


def reset(token) -> None:
    _words.reset(token)


def current() -> str | None:
    bound = _words.get()
    return bound[0] if bound else None


_MORE_RE = re.compile(
    r"\b(?:another|one more|more of|again|extra|second one|twice|a second)\b"
    r"|\b\d+\s*(?:more|of them|of those|of these|x)\b"
)


def this_turn() -> str:
    """What the shopper said this turn, lowercased; "" when unbound."""
    bound = _words.get()
    return bound[1] if bound else ""


# Something like it is already in the bag: keep both, replace it, or leave this
# one out. Read off the shopper's own words, the chips included.
_SKIP_RE = re.compile(r"\b(?:don'?t add|do not add|cancel (?:it|this|that)|skip (?:it|this|that)|never ?mind|"
                      r"leave (?:it|this one) out|not this one)\b")
_REPLACE_RE = re.compile(r"\b(?:replace|swap|instead|in place of|remove the (?:other|previous|old|first|one))\b")
_BOTH_RE = re.compile(r"\b(?:keep both|both|as well|too|also)\b")


def similar_decision() -> str | None:
    """"skip", "replace", "both" - or None when this turn did not say."""
    text = this_turn()
    if _SKIP_RE.search(text):
        return "skip"
    if _REPLACE_RE.search(text):
        return "replace"
    if _BOTH_RE.search(text):
        return "both"
    return None


def asks_for_more() -> bool:
    """Whether this turn's message asks for more of something - "another one",
    "one more", "again". Unbound, nothing is held back."""
    bound = _words.get()
    return True if bound is None else bool(_MORE_RE.search(bound[1]))


def _found(pattern: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9']){pattern}(?![a-z0-9])", text) is not None


_LETTER_SIZES = {
    "xxs": ("extra extra small",), "xs": ("extra small",), "s": ("small",),
    "m": ("medium",), "l": ("large",), "xl": ("extra large",), "xxl": ("extra extra large",),
}


def _size_patterns(part: str) -> list[str]:
    """The ways a shopper writes one size: "4Y" is "4y", "4 years", "size 4"."""
    part = part.strip().lower()
    patterns = [re.escape(part)]
    # 4Y, 4-5Y: years - never "4 year old", which is an age, not a size choice.
    years = re.fullmatch(r"(\d+(?:\s*-\s*\d+)?)\s*y(?:rs?|ears?)?", part)
    if years:
        n = re.escape(years.group(1).replace(" ", ""))
        patterns += [rf"{n}\s*(?:y|yr|yrs|year|years)(?!\s*-?\s*old)", rf"size\s*{n}"]
        return patterns
    months = re.fullmatch(r"(\d+(?:\s*-\s*\d+)?)\s*m(?:ths?|onths?)?", part)
    if months:
        n = re.escape(months.group(1).replace(" ", ""))
        patterns += [rf"{n}\s*(?:m|mth|mths|month|months)(?!\s*-?\s*old)", rf"size\s*{n}\s*m"]
        return patterns
    shoe = re.fullmatch(r"(\d+(?:\.\d)?)\s*(eu|uk|us)", part)
    if shoe:
        n, unit = re.escape(shoe.group(1)), shoe.group(2)
        patterns += [rf"{n}\s*{unit}", rf"{unit}\s*{n}", rf"size\s*{n}"]
        if unit == "eu":
            patterns.append(rf"(?<![\d.,]){n}(?![\d.,])")   # "35" to "which size?"
        return patterns
    length = re.fullmatch(r"(\d+)\s*cm", part)
    if length:
        patterns.append(rf"{re.escape(length.group(1))}\s*cm")
        return patterns
    if part in _LETTER_SIZES:
        patterns += [re.escape(w) for w in _LETTER_SIZES[part]] + [rf"size\s*{part}"]
    return patterns


def said_size(value: str, text: str) -> bool:
    """Whether the shopper named this size, in any of the ways sizes get written."""
    parts = [value, *re.split(r"\s*/\s*", value)]
    return any(_found(p, text) for part in parts if part.strip() for p in _size_patterns(part))


_SPELLINGS = {"grey": "gray", "gray": "grey"}


def said_colour(value: str, text: str) -> bool:
    """Whether the shopper named this colour - "the blue one" names Blue.

    The whole name, or its last word, which is the hue: "green" names Racing
    Green. Never an earlier word - "for my baby" does not choose Baby Pink.
    """
    if _found(re.escape(value.strip().lower()), text):
        return True
    words = re.findall(r"[a-z]+", value.lower())
    hue = words[-1] if words else ""
    if len(hue) < 3:
        return False
    return _found(hue, text) or (hue in _SPELLINGS and _found(_SPELLINGS[hue], text))
