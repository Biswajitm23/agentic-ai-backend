"""Everything the store says about one product, for questions search cannot answer.

"What fabric is it?", "is it machine washable?", "is it warm enough for winter?",
"where is it made?", "which sizes are left?" - the answers are in the product's
own description and variants, which the search and browse tools leave out to
keep their results small. So the model was answering from whichever tool it
happened to call, and said "100% wool" one minute and "I don't have its fabric
details" the next about the same jumper.

The product is found the way compare_products finds one - exact title, then
every word the shopper used, then a close spelling - so "the green jumper" and
"Cable Knit Jumper" land on the same piece. Only ACTIVE products are returned.
"""

import html
import re

from app.services.compare import _fabric, _highlights, _made_in, resolve
from app.services.shopify_client import graphql
from app.services.shopify_storefront import (
    collection_tree,
    product_image,
    product_url,
    sale_summary,
    shop_info,
    suits,
)

PRODUCT_DETAILS = """
query SupportProductDetails($id: ID!) {
  product(id: $id) {
    legacyResourceId
    title
    handle
    productType
    tags
    status
    onlineStoreUrl
    descriptionHtml
    featuredMedia { ... on MediaImage { image { url altText } } }
    options { name values }
    variants(first: 100) {
      nodes {
        legacyResourceId
        title
        price
        compareAtPrice
        availableForSale
        selectedOptions { name value }
      }
    }
  }
}
"""

DESCRIPTION_LIMIT = 900        # characters of prose; the bullets come separately
_TAG_RE = re.compile(r"<[^>]+>")
_LI_BLOCK_RE = re.compile(r"<li[^>]*>.*?</li>", re.S | re.I)
_BLOCK_END_RE = re.compile(r"</(?:p|div|h\d|ul|ol)>|<br\s*/?>", re.I)
_REF_RE = re.compile(r"\bRef\.?\s*Code:.*$", re.I | re.M)
# Care wording as the store writes it: "Wipe clean with care", "Hand wash only".
_CARE_RE = re.compile(
    r"\b(?:wash|washable|wipe|clean|dry\s*clean|tumble|iron|bleach|spot\s*clean|care)\b", re.I
)


def _prose(description_html: str | None) -> str:
    """The description as plain sentences: bullets and the internal ref code removed."""
    text = _LI_BLOCK_RE.sub(" ", description_html or "")
    text = _BLOCK_END_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = _REF_RE.sub("", text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > DESCRIPTION_LIMIT:
        cut = text.rfind(". ", 0, DESCRIPTION_LIMIT)
        text = text[: cut + 1 if cut > 200 else DESCRIPTION_LIMIT].rstrip() + " …"
    return text


def _care(highlights: list[str], prose: str) -> list[str]:
    """Care instructions, from the bullets first, then any sentence that gives one."""
    found = [h for h in highlights if _CARE_RE.search(h)]
    if not found:
        found = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prose) if _CARE_RE.search(s)][:2]
    return found


def _option(variant: dict, *names: str) -> str | None:
    for o in variant.get("selectedOptions") or []:
        if o["name"].strip().lower() in names:
            return o["value"]
    return None


def _details(node: dict, currency: str) -> dict:
    variants = node["variants"]["nodes"]
    highlights = _highlights(node.get("descriptionHtml"))
    prose = _prose(node.get("descriptionHtml"))
    options = {o["name"].strip().lower(): o["values"] for o in node.get("options") or []}
    prices = [float(v["price"]) for v in variants if v.get("price") is not None]

    # Which sizes can be bought, per colour when there is more than one: "4Y is
    # sold out in Navy" is the answer a shopper needs, not a flat list.
    by_colour: dict[str, dict[str, list[str]]] = {}
    for v in variants:
        colour = _option(v, "color", "colour") or "-"
        size = _option(v, "size") or (None if v["title"] == "Default Title" else v["title"])
        if size is None:
            continue
        slot = by_colour.setdefault(colour, {"in_stock": [], "sold_out": []})
        slot["in_stock" if v["availableForSale"] else "sold_out"].append(size)

    buyable = next((v for v in variants if v["availableForSale"]), variants[0] if variants else {})
    return {
        "product_id": node.get("legacyResourceId"),
        "variant_id": buyable.get("legacyResourceId"),
        "title": node["title"],
        "handle": node["handle"],
        "category": node.get("productType") or None,
        "for": suits(node.get("tags")),
        "url": product_url(node),
        "image": product_image(node),
        "currency": currency,
        "price_from": round(min(prices), 2) if prices else None,
        "price_to": round(max(prices), 2) if prices else None,
        **sale_summary(variants),
        "in_stock": any(v["availableForSale"] for v in variants),
        # The store's own words. Everything below is read out of these two.
        "description": prose or None,
        "highlights": highlights,
        "fabric": _fabric(highlights, prose),
        "made_in": _made_in(highlights, prose),
        "care": _care(highlights, prose),
        "colours": options.get("color") or options.get("colour") or [],
        "sizes": options.get("size") or [],
        "stock_by_colour": by_colour if len(by_colour) > 1 or "-" not in by_colour else None,
        "sizes_in_stock": None if len(by_colour) != 1 else next(iter(by_colour.values()))["in_stock"],
        "sizes_sold_out": None if len(by_colour) != 1 else next(iter(by_colour.values()))["sold_out"],
    }


async def product_details(product: str) -> dict:
    """The full detail of one product a shopper named, or the nearest names if none matched."""
    name = " ".join(str(product or "").split())
    if not name:
        return {"found": False, "reason": "no_product_named"}
    tree = await collection_tree()
    product_id, near = resolve(name, tree["ids"], tree["titles"])
    if product_id is None:
        return {"found": False, "reason": "not_found", "asked_for": name, "did_you_mean": near}

    currency = (await shop_info())["currency"]
    node = (await graphql(PRODUCT_DETAILS, {"id": f"gid://shopify/Product/{product_id}"}))["product"]
    if not node or node.get("status") != "ACTIVE":
        return {"found": False, "reason": "not_found", "asked_for": name, "did_you_mean": near}
    details = _details(node, currency)
    # "products" as well, so the storefront draws its card like any other result.
    return {"found": True, "currency": currency, "product": details, "products": [details]}
