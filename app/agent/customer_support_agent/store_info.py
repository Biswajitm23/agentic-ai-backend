"""Static store information the support agent is allowed to quote.

PLACEHOLDER CONTENT — edit this to match the store's published policies before
putting the agent in front of real customers. The agent is instructed to quote
policy only from this tool, so whatever is written here is what shoppers are
told, word for word.
"""

STORE_POLICIES = {
    "shipping": {
        "processing_time": "Orders are packed within 1-2 business days.",
        "domestic_delivery": "3-5 business days after dispatch.",
        "international_delivery": "7-14 business days after dispatch.",
        "free_shipping_threshold": "Free standard shipping on orders over $75.",
        "tracking": "A tracking link is emailed as soon as the order ships.",
    },
    "returns": {
        "window": "Returns accepted within 30 days of delivery.",
        "condition": "Items must be unopened and in their original packaging.",
        "refund_time": "Refunds are issued to the original payment method within 5-7 business days of us receiving the return.",
        "exclusions": "Opened supplements and clearance items cannot be returned.",
    },
    "payment": {
        "methods": "All major credit and debit cards, plus Shop Pay, Apple Pay and Google Pay.",
        "currency": "All prices are shown and charged in USD.",
    },
    "contact": {
        "email": "support@example.com",
        "hours": "Monday to Friday, 9am-5pm.",
        "escalation": "A human agent replies to email within one business day.",
    },
}
