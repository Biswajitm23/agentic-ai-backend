"""Keep the store's contact details true in every reply.

Told to "email the team", the model once wrote hello@palashstor.com - an address
that does not exist - instead of the store's own support@littlenest.com. A
shopper who writes there waits for an answer that never comes. So the real
address (Shopify's shop.contactEmail) rides along with every turn, and any
other address in a reply is replaced with it before the reply is shown or saved.

Addresses the shopper gave themselves - the email on their order, say - are
left alone: repeating "test@example.com" back to the person who typed it is
not an invented contact.
"""

import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def emails_in(*texts: str | None) -> set[str]:
    return {m.group(0).lower() for t in texts if t for m in _EMAIL_RE.finditer(t)}


def briefing(contact_email: str | None) -> str | None:
    """The note that tells the agent which address is the store's."""
    if not contact_email:
        return None
    return (f"[Store contact: {contact_email} - the only email address you may give for the store "
            f"or its team]")


def fix_reply(reply: str, contact_email: str | None, shopper_said: set[str]) -> tuple[str, list[str]]:
    """The reply with every invented address swapped for the store's, and what was swapped.

    With no known contact address there is nothing true to put in its place, so
    an invented one is removed rather than left standing.
    """
    real = (contact_email or "").lower()
    allowed = {real, *shopper_said} - {""}
    replaced: list[str] = []

    def swap(match: re.Match) -> str:
        found = match.group(0)
        if found.lower() in allowed:
            return found
        replaced.append(found)
        return contact_email or "our support team"

    return _EMAIL_RE.sub(swap, reply or ""), replaced
