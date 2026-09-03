"""Letting a shopper cancel an order or move its delivery address, from the chat.

Everything else the support agent touches is a read. These are writes: one
refunds money and restocks goods, the other redirects a parcel that has already
been paid for. So they are deliberately harder to reach than a status lookup.

The gate is two-step and server-side:

1. ``begin`` finds the order the same non-probing way ``find_order`` does - the
   order number and the email must match, and a wrong email is indistinguishable
   from an order that does not exist. It checks the change is even possible, then
   mints a short-lived ticket and returns a *challenge*: one detail printed on the
   order that the buyer has and a guesser does not. The answer is never returned.

2. ``commit`` takes the ticket and the answer. Wrong answers burn attempts, the
   ticket dies after three, and it expires on its own after a few minutes. Only
   then does a mutation run.

Why a challenge at all: the storefront widget sends ``verified: false`` on
purpose, because a browser posting an email proves nothing. Order numbers are
close to sequential and email addresses leak, so those two together are a weak
secret - fine for showing someone where their parcel is, not for moving it. The
postcode already on the order is knowledge the buyer holds; asking for it turns
"knows two guessable things" into "is holding the confirmation email".

Tickets live in this process only. A restart or a second worker loses them, and
a shopper simply starts again - which is why nothing irreversible is stored in
one, only a pointer to an order that has already been matched.
"""

import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.shopify_client import ShopifyError, graphql
from app.services.shopify_storefront import order_number_variants

logger = logging.getLogger(__name__)

CANCEL = "cancel"
CHANGE_ADDRESS = "change_address"
ACTIONS = (CANCEL, CHANGE_ADDRESS)

# What we offer rather than making someone invent a phrase. `other` is what
# makes the free-text box legitimate instead of a fallback nobody reaches.
CANCEL_REASONS = [
    {"code": "changed_mind", "label": "I changed my mind"},
    {"code": "wrong_item", "label": "I ordered the wrong item or size"},
    {"code": "ordered_twice", "label": "I ordered twice by mistake"},
    {"code": "too_slow", "label": "It is taking too long to arrive"},
    {"code": "found_cheaper", "label": "I found it cheaper elsewhere"},
    {"code": "no_longer_needed", "label": "I no longer need it"},
    {"code": "other", "label": "Something else"},
]

ADDRESS_REASONS = [
    {"code": "typo", "label": "I mistyped the address"},
    {"code": "moved", "label": "I have moved since ordering"},
    {"code": "send_elsewhere", "label": "Send it to a different address"},
    {"code": "work_address", "label": "Send it to my work address"},
    {"code": "gift", "label": "It is a gift and should go to them"},
    {"code": "other", "label": "Something else"},
]

_REASON_CODES = {r["code"] for r in CANCEL_REASONS} | {r["code"] for r in ADDRESS_REASONS}

# Shopify's own enum is coarse. Every one of ours is the customer asking, so the
# shopper's actual words go in the staff note where a human will read them.
SHOPIFY_CANCEL_REASON = "CUSTOMER"

# Cancelling refunds to the original payment method and restocks. Both are what
# a shopper means by "cancel", and leaving either off quietly turns a
# cancellation into a support ticket. Store credit is the other option Shopify
# offers, and is not what someone asking to cancel is expecting.
REFUND_ON_CANCEL = True
RESTOCK_ON_CANCEL = True
NOTIFY_ON_CANCEL = True

REASON_NOTE_LIMIT = 400

# Fulfilment states where nothing has physically left yet.
UNSTARTED = {"UNFULFILLED", "ON_HOLD", "SCHEDULED", "OPEN"}


ORDER_FOR_CHANGE = """
query SupportOrderForChange($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      email
      note
      tags
      createdAt
      cancelledAt
      displayFulfillmentStatus
      displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      shippingAddress {
        firstName lastName address1 address2 city zip
        provinceCode countryCodeV2 phone
      }
      lineItems(first: 10) { nodes { title quantity } }
    }
  }
}
"""

# `refundMethod` replaced the old boolean `refund` argument - passing the old
# shape is rejected outright, so this operation is tied to a current API version.
ORDER_CANCEL = """
mutation SupportOrderCancel(
  $orderId: ID!, $reason: OrderCancelReason!, $refundMethod: OrderCancelRefundMethodInput!,
  $restock: Boolean!, $staffNote: String, $notifyCustomer: Boolean
) {
  orderCancel(
    orderId: $orderId, reason: $reason, refundMethod: $refundMethod,
    restock: $restock, staffNote: $staffNote, notifyCustomer: $notifyCustomer
  ) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}
"""

ORDER_UPDATE_ADDRESS = """
mutation SupportOrderAddress($input: OrderInput!) {
  orderUpdate(input: $input) {
    order {
      id
      name
      shippingAddress { address1 address2 city zip provinceCode countryCodeV2 }
    }
    userErrors { field message }
  }
}
"""


# ── Tickets ────────────────────────────────────────────────────────────────


@dataclass
class Ticket:
    """One shopper, one order, one pending change, for a few minutes."""

    order_id: str
    order_name: str
    action: str
    email: str
    session_id: str | None
    challenge_kind: str
    challenge_answer: str
    current_address: dict
    note: str | None
    created_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    verified: bool = False


_tickets: dict[str, Ticket] = {}


def _ttl_seconds() -> float:
    return settings.SUPPORT_CHANGE_TTL_MINUTES * 60


def _sweep() -> None:
    """Drop anything past its life. Cheap - there are never many of these."""
    cutoff = time.monotonic() - _ttl_seconds()
    for token in [t for t, tk in _tickets.items() if tk.created_at < cutoff]:
        _tickets.pop(token, None)


def _get(token: str, session_id: str | None) -> Ticket | None:
    _sweep()
    ticket = _tickets.get((token or "").strip())
    if ticket is None:
        return None
    # A ticket belongs to the conversation it was minted in. Without this, a
    # token that leaked out of one transcript would work in another.
    if ticket.session_id and session_id and ticket.session_id != session_id:
        logger.warning("Order-change ticket used from a different session")
        return None
    return ticket


# ── Normalising answers ────────────────────────────────────────────────────


def _norm_postcode(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _norm_money(value: str) -> str:
    """Strip symbols, thousands separators and currency codes, keep the number."""
    digits = re.sub(r"[^0-9.]", "", (value or ""))
    try:
        return f"{float(digits):.2f}"
    except ValueError:
        return ""


def _challenge_for(node: dict) -> tuple[str, str, str]:
    """Pick what to ask, and what the right answer is. Never leaks the answer."""
    address = node.get("shippingAddress") or {}
    postcode = (address.get("zip") or "").strip()
    if postcode:
        return (
            "postcode",
            _norm_postcode(postcode),
            "the postcode on the delivery address for that order",
        )

    money = node["totalPriceSet"]["shopMoney"]
    return (
        "total",
        _norm_money(money["amount"]),
        f"the order total, in {money['currencyCode']}",
    )


def _answer_matches(ticket: Ticket, given: str) -> bool:
    if ticket.challenge_kind == "postcode":
        return _norm_postcode(given) == ticket.challenge_answer
    return _norm_money(given) == ticket.challenge_answer


# ── Eligibility ────────────────────────────────────────────────────────────


def _days_old(created_at: str) -> float:
    from datetime import datetime, timezone

    try:
        placed = datetime.fromisoformat((created_at or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - placed).total_seconds() / 86400


def _eligibility(node: dict, action: str) -> dict | None:
    """None when the change can go ahead, otherwise why it cannot."""
    if node.get("cancelledAt"):
        return {
            "reason": "already_cancelled",
            "tell_customer": "That order has already been cancelled.",
        }

    status = (node.get("displayFulfillmentStatus") or "").upper()
    if status not in UNSTARTED:
        if action == CANCEL:
            return {
                "reason": "already_fulfilled",
                "tell_customer": (
                    "That order has already been packed and sent, so it is too late to "
                    "cancel it. You can send it back once it arrives."
                ),
            }
        return {
            "reason": "already_fulfilled",
            "tell_customer": (
                "That order is already on its way, so the address cannot be changed now. "
                "The carrier may still be able to redirect it."
            ),
        }

    if action == CANCEL:
        window = settings.SUPPORT_CANCEL_WINDOW_DAYS
        if window and _days_old(node.get("createdAt")) > window:
            return {
                "reason": "outside_window",
                "tell_customer": (
                    f"That order was placed more than {window} days ago, so I cannot cancel "
                    "it here. Let me put you through to someone who can help."
                ),
            }

    return None


# ── Step one ───────────────────────────────────────────────────────────────


async def begin(order_number: str, email: str, action: str, session_id: str | None = None) -> dict:
    """Match the order, check the change is possible, and mint a ticket.

    Returns what the agent needs to run the conversation - the order summary, the
    reasons to offer, and the question to put to the shopper - and nothing that
    would help someone who has not got the order in front of them.
    """
    if not settings.SUPPORT_ORDER_CHANGES:
        return {
            "available": False,
            "tell_customer": (
                "I cannot change orders from here. I can pass this to the team, who can."
            ),
        }

    if action not in ACTIONS:
        return {"error": "unknown_action", "actions": list(ACTIONS)}

    given_email = (email or "").strip().casefold()
    if not given_email or "@" not in given_email:
        return {"found": False, "reason": "email_required"}

    node = None
    for candidate in order_number_variants(order_number):
        data = await graphql(ORDER_FOR_CHANGE, {"query": f"name:{candidate}"})
        nodes = data["orders"]["nodes"]
        if nodes:
            node = nodes[0]
            break

    # Same answer for a wrong email as for an order that is not there, so this
    # cannot be used to find out which order numbers exist.
    if node is None or (node.get("email") or "").strip().casefold() != given_email:
        return {"found": False, "reason": "no_match"}

    blocked = _eligibility(node, action)
    if blocked:
        return {"found": True, "eligible": False, "order_number": node["name"], **blocked}

    kind, answer, phrasing = _challenge_for(node)
    token = secrets.token_urlsafe(24)

    _sweep()
    _tickets[token] = Ticket(
        order_id=node["id"],
        order_name=node["name"],
        action=action,
        email=given_email,
        session_id=session_id,
        challenge_kind=kind,
        challenge_answer=answer,
        current_address=node.get("shippingAddress") or {},
        note=node.get("note"),
    )

    money = node["totalPriceSet"]["shopMoney"]
    address = node.get("shippingAddress") or {}
    return {
        "found": True,
        "eligible": True,
        "change_token": token,
        "action": action,
        "order_number": node["name"],
        "placed_on": (node.get("createdAt") or "")[:10] or None,
        "total": round(float(money["amount"]), 2),
        "currency": money["currencyCode"],
        "items": [
            {"title": li["title"], "quantity": li["quantity"]}
            for li in node["lineItems"]["nodes"]
        ],
        # Enough for the shopper to recognise the address without printing it in
        # full at someone who may not be entitled to see it.
        "delivering_to": ", ".join(
            p for p in [address.get("city"), address.get("countryCodeV2")] if p
        )
        or None,
        "verification_required": settings.SUPPORT_VERIFY_ORDER_CHANGES,
        "ask_shopper_for": phrasing if settings.SUPPORT_VERIFY_ORDER_CHANGES else None,
        "reasons": CANCEL_REASONS if action == CANCEL else ADDRESS_REASONS,
        "expires_in_minutes": settings.SUPPORT_CHANGE_TTL_MINUTES,
    }


# ── Step two ───────────────────────────────────────────────────────────────


def _reason_note(action: str, reason_code: str, reason_text: str) -> str:
    catalogue = CANCEL_REASONS if action == CANCEL else ADDRESS_REASONS
    label = next((r["label"] for r in catalogue if r["code"] == reason_code), None)

    said = (reason_text or "").strip()[:REASON_NOTE_LIMIT]
    parts = [p for p in [label, f'"{said}"' if said else None] if p]
    return " - ".join(parts) or "No reason given"


def _address_input(ticket: Ticket, new_address: dict) -> dict:
    """Build a MailingAddressInput, keeping whatever the shopper did not change.

    Country and province stay as they were unless a new code is supplied: a
    shopper correcting a street name should not silently move their parcel to
    another country because a field arrived empty.
    """
    current = ticket.current_address or {}
    given = {k: (v or "").strip() for k, v in (new_address or {}).items() if v}

    address = {
        "address1": given.get("address1") or current.get("address1"),
        "address2": given.get("address2", current.get("address2")),
        "city": given.get("city") or current.get("city"),
        "zip": given.get("zip") or current.get("zip"),
        "firstName": given.get("first_name") or current.get("firstName"),
        "lastName": given.get("last_name") or current.get("lastName"),
        "phone": given.get("phone") or current.get("phone"),
        "provinceCode": given.get("province_code") or current.get("provinceCode"),
        "countryCode": given.get("country_code") or current.get("countryCodeV2"),
    }
    return {k: v for k, v in address.items() if v}


async def commit(
    token: str,
    verification_answer: str = "",
    reason_code: str = "",
    reason_text: str = "",
    new_address: dict | None = None,
    session_id: str | None = None,
) -> dict:
    """Verify the ticket, then actually cancel or move the order."""
    ticket = _get(token, session_id)
    if ticket is None:
        return {
            "done": False,
            "reason": "expired",
            "tell_customer": (
                "That request has expired. Give me the order number and email again and "
                "we can pick it back up."
            ),
        }

    if settings.SUPPORT_VERIFY_ORDER_CHANGES and not ticket.verified:
        if not _answer_matches(ticket, verification_answer):
            ticket.attempts += 1
            left = settings.SUPPORT_CHANGE_MAX_ATTEMPTS - ticket.attempts
            if left <= 0:
                _tickets.pop(token, None)
                logger.warning("Order-change ticket burned after too many attempts")
                return {
                    "done": False,
                    "reason": "too_many_attempts",
                    "tell_customer": (
                        "That did not match what we have on the order, so I have stopped "
                        "there. Please contact us and we will sort it out properly."
                    ),
                }
            return {
                "done": False,
                "reason": "verification_failed",
                "attempts_left": left,
                "tell_customer": "That does not match what we have on the order - try again?",
            }
        ticket.verified = True

    if reason_code and reason_code not in _REASON_CODES:
        return {"done": False, "reason": "unknown_reason_code", "reasons": CANCEL_REASONS}

    note = _reason_note(ticket.action, reason_code, reason_text)

    try:
        if ticket.action == CANCEL:
            result = await _do_cancel(ticket, note)
        else:
            result = await _do_address(ticket, note, new_address or {})
    except (ShopifyError, KeyError, ValueError) as exc:
        logger.warning("Order change %s failed for %s: %s", ticket.action, ticket.order_name, exc)
        return {
            "done": False,
            "reason": "store_error",
            "tell_customer": (
                "I could not put that through just now. Please try again in a minute, or "
                "contact us and we will do it for you."
            ),
        }

    # One ticket, one write. Spent either way, so a retry has to start over.
    _tickets.pop(token, None)
    return result


async def _do_cancel(ticket: Ticket, note: str) -> dict:
    data = await graphql(
        ORDER_CANCEL,
        {
            "orderId": ticket.order_id,
            "reason": SHOPIFY_CANCEL_REASON,
            "refundMethod": {"originalPaymentMethodsRefund": REFUND_ON_CANCEL},
            "restock": RESTOCK_ON_CANCEL,
            "staffNote": f"Cancelled by the shopper in chat. Reason: {note}",
            "notifyCustomer": NOTIFY_ON_CANCEL,
        },
    )
    payload = data["orderCancel"]
    errors = payload.get("orderCancelUserErrors") or []
    if errors:
        logger.warning("orderCancel rejected %s: %s", ticket.order_name, errors)
        return {
            "done": False,
            "reason": "rejected",
            "tell_customer": (
                "Our system would not let me cancel that one. Let me pass you to someone "
                "who can look at it properly."
            ),
        }

    return {
        "done": True,
        "action": CANCEL,
        "order_number": ticket.order_name,
        # Shopify cancels asynchronously; the refund follows the queue.
        "refund_started": REFUND_ON_CANCEL,
        "tell_customer": (
            f"Order {ticket.order_name} is cancelled. The refund goes back to your original "
            "payment method and usually lands within a few working days - you will get an "
            "email confirming it."
        ),
    }


async def _do_address(ticket: Ticket, note: str, new_address: dict) -> dict:
    address = _address_input(ticket, new_address)
    if not address.get("address1"):
        return {
            "done": False,
            "reason": "address_required",
            "tell_customer": "I need the new address before I can move it.",
        }

    existing = (ticket.note or "").strip()
    trail = f"Delivery address changed by the shopper in chat. Reason: {note}"

    data = await graphql(
        ORDER_UPDATE_ADDRESS,
        {
            "input": {
                "id": ticket.order_id,
                "shippingAddress": address,
                "note": f"{existing}\n{trail}".strip(),
            }
        },
    )
    payload = data["orderUpdate"]
    errors = payload.get("userErrors") or []
    if errors:
        logger.warning("orderUpdate rejected %s: %s", ticket.order_name, errors)
        return {
            "done": False,
            "reason": "rejected",
            "message": "; ".join(e.get("message", "") for e in errors),
            "tell_customer": (
                "That address was not accepted - check the street and postcode and I will "
                "try again."
            ),
        }

    saved = (payload.get("order") or {}).get("shippingAddress") or {}
    return {
        "done": True,
        "action": CHANGE_ADDRESS,
        "order_number": ticket.order_name,
        "new_address": ", ".join(
            p
            for p in [
                saved.get("address1"),
                saved.get("address2"),
                saved.get("city"),
                saved.get("zip"),
            ]
            if p
        ),
        "tell_customer": (
            f"Done - order {ticket.order_name} will now go to the new address. It has not "
            "shipped yet, so nothing else changes."
        ),
    }
