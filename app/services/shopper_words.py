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

_words: ContextVar[str | None] = ContextVar("shopper_words", default=None)


def set_words(messages: list[str]):
    """Bind the shopper's messages for this turn. Returns a token for ``reset``."""
    text = " ".join(" ".join(m.lower().replace("’", "'").split()) for m in messages if m)
    return _words.set(text)


def reset(token) -> None:
    _words.reset(token)


def current() -> str | None:
    return _words.get()


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
