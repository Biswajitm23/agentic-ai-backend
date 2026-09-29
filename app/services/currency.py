"""Budgets and prices a shopper gives in a currency the store does not charge in.

The store sells in one currency (INR today - `shop.enabledPresentmentCurrencies`
holds nothing else), so every price a tool returns is in it. A shopper who says
"a christening outfit under 150 pounds" used to have the 150 read as rupees, or
ignored: they were shown a 24,200 INR dress. This finds the amount and the
currency in their own words and converts it, so the agent can filter in the
store currency and say plainly that the conversion is approximate.

Rates are configured, not fetched (SUPPORT_FX_RATES: shop-currency units per one
unit of each foreign currency). They are for sizing a budget only - checkout
always charges the store currency, and the agent is told to say so.
"""

import re

from app.core.config import settings

# How shoppers write each currency. Checked as whole words / symbols only.
_NAMES = {
    "GBP": r"pounds?|quid|gbp|£",
    "USD": r"dollars?|bucks|usd|us\$|\$",
    "EUR": r"euros?|eur|€",
    "AED": r"dirhams?|aed",
    "AUD": r"aud|a\$|australian dollars?",
    "CAD": r"cad|c\$|canadian dollars?",
    "SGD": r"sgd|s\$|singapore dollars?",
    "NZD": r"nzd|nz\$|new zealand dollars?",
    "JPY": r"yen|jpy|¥",
    "CNY": r"yuan|rmb|cny",
    "CHF": r"swiss francs?|chf",
    "SAR": r"riyals?|sar",
    "QAR": r"qar",
    "ZAR": r"rand|zar",
    "INR": r"rupees?|rs\.?|inr|₹",
}
# Longer, more specific spellings first: "australian dollars" before "dollars".
# Recognised without a configured rate is still useful: the agent is told to ask
# for the budget in the store currency rather than guess a conversion.
_ORDER = ["AUD", "CAD", "SGD", "NZD", "GBP", "EUR", "AED", "JPY", "CNY", "CHF", "SAR", "QAR", "ZAR",
          "INR", "USD"]
_ANY = "|".join(f"(?P<{code}>{_NAMES[code]})" for code in _ORDER)
_NUMBER = r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<k>k\b)?"
# "£150", "$ 1,200", "150 pounds", "150k rupees", "EUR 80".
_BEFORE_RE = re.compile(rf"(?<![\w$])(?:{_ANY})\s*{_NUMBER}", re.I)
_AFTER_RE = re.compile(rf"{_NUMBER}\s*(?:{_ANY})(?![\w])", re.I)
# "how much is that in dollars", "price in pounds": a currency named, no amount.
_MENTION_RE = re.compile(rf"\b(?:in|into|to)\s+(?:{_ANY})(?![\w])", re.I)


def _code(match: re.Match) -> str | None:
    return next((c for c in _ORDER if match.group(c)), None)


def _amount(match: re.Match) -> float:
    value = float(match.group("amount").replace(",", ""))
    return value * 1000 if match.group("k") else value


def find(text: str) -> dict | None:
    """The first foreign amount in the text, or a currency they asked about.

    {"amount": 150.0, "currency": "GBP"} for "under 150 pounds";
    {"amount": None, "currency": "USD"} for "how much is it in dollars";
    None when they wrote nothing in a foreign currency (a rupee budget included).
    """
    text = text or ""
    hits = [m for m in (*_BEFORE_RE.finditer(text), *_AFTER_RE.finditer(text))]
    hits.sort(key=lambda m: m.start())
    for match in hits:
        code = _code(match)
        if code and code != _shop_currency_guess():
            return {"amount": _amount(match), "currency": code}
        if code:
            return None          # a budget already in the store's own currency
    mention = _MENTION_RE.search(text)
    if mention:
        code = _code(mention)
        if code and code != _shop_currency_guess():
            return {"amount": None, "currency": code}
    return None


def _shop_currency_guess() -> str:
    # The configured rates are relative to this; INR is the store's currency.
    return settings.SUPPORT_FX_BASE_CURRENCY.upper()


def rate(code: str) -> float | None:
    """Shop-currency units for one unit of `code`, or None when no rate is configured."""
    value = (settings.SUPPORT_FX_RATES or {}).get(code.upper())
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def to_shop(amount: float, code: str) -> float | None:
    r = rate(code)
    return round(amount * r, -2) if r else None      # to the nearest hundred: it is approximate


def briefing(text: str, shop_currency: str) -> str | None:
    """A note for the agent when the shopper used a foreign currency, else None."""
    found = find(text)
    if not found or shop_currency.upper() != _shop_currency_guess():
        return None
    code, amount = found["currency"], found["amount"]
    r = rate(code)
    if amount is None:
        if not r:
            return (f"[They mentioned {code}. We charge only in {shop_currency} and have no rate "
                    f"for {code}: give prices in {shop_currency} and say checkout is in {shop_currency}]")
        return (f"[They asked about {code}. We charge only in {shop_currency}; roughly 1 {code} = "
                f"{r:g} {shop_currency}. Give prices in {shop_currency}, you may add the approximate "
                f"{code} figure, and say checkout is in {shop_currency}]")
    if not r:
        return (f"[Their budget is {amount:g} {code}, but we charge only in {shop_currency} and have no "
                f"rate for {code}: ask for the budget in {shop_currency}]")
    converted = to_shop(amount, code)
    return (f"[Their budget is {amount:g} {code}, roughly {converted:,.0f} {shop_currency} (approximate "
            f"rate 1 {code} = {r:g} {shop_currency}). We charge only in {shop_currency}: use "
            f"{converted:.0f} as the budget in every tool, and tell them once that prices are in "
            f"{shop_currency} and the conversion is approximate]")
