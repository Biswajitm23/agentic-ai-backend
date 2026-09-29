"""Which goes first under a reply: the answer buttons or the product cards.

The widget draws a reply's product cards and then its suggestion chips. That
suits a reply whose products are the answer, but not one whose point is a
question - "Here are a few picks. How old is she?" - where the shopper should
meet the answers (Under 1, 3 years, ...) before a shelf they have not narrowed
yet. Only the model knows which kind of reply it wrote, so it says so: a reply
that should lead with the buttons starts with MARKER. The marker is taken out
of everything the shopper or the transcript sees; the choice travels as the
``position`` of the suggestions event instead.
"""

import re

MARKER = "<<options_first>>"
BEFORE_PRODUCTS = "before_products"
AFTER_PRODUCTS = "after_products"

# Tolerant of the ways a model mistypes it: spaces, a hyphen, capitals.
_MARKER_RE = re.compile(r"<<\s*options[\s_-]*first\s*>>\s*", re.I)


def split(reply: str) -> tuple[str, str]:
    """The reply without the marker, and where its suggestions go.

    The model's marker decides; a reply whose last line is a question counts too,
    because that is what the marker is for and the model sometimes forgets it -
    "Want me to add one to your bag?" left its Yes/No under the card.
    """
    text, found = _MARKER_RE.subn("", reply or "")
    text = text.strip()
    last_line = text.splitlines()[-1].strip() if text else ""
    asks = last_line.endswith("?")
    return text, BEFORE_PRODUCTS if found or asks else AFTER_PRODUCTS


class MarkerFilter:
    """Takes the marker out of a streamed reply as it arrives.

    A token may end halfway through the marker, so only a tail that could still
    be its start is held back; everything else passes straight through. The
    finished reply is cleaned by ``split`` - this only keeps the marker from
    flashing up while the shopper watches it type.
    """

    def __init__(self) -> None:
        self._held = ""

    def feed(self, text: str) -> str:
        buf = _MARKER_RE.sub("", self._held + text)
        keep = next((k for k in range(min(len(buf), len(MARKER) - 1), 0, -1)
                     if MARKER.startswith(buf[-k:].lower())), 0)
        self._held = buf[len(buf) - keep:] if keep else ""
        return buf[: len(buf) - keep]

    def reset(self) -> None:
        """Drop what was held: the text it belonged to was just discarded."""
        self._held = ""

    def flush(self) -> str:
        """Whatever was held back and turned out not to be the marker."""
        held, self._held = self._held, ""
        return held
