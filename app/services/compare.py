"""Putting two or more products side by side.

The agent writes the comparison but is never trusted to find the facts or do the
sums. Each name is resolved here to a real, buyable product; its facts come from
the product's own data - options, tags, and the bullet points in its
description; and the differences - which is dearer and by how much, which sizes
and colours they share - are worked out in code.
"""

import difflib
import html
import re

from app.services.shopify_client import graphql
from app.services.shopify_storefront import collection_tree, product_image, product_url, shop_info
from app.services.suggestions import plural

MAX_COMPARE = 4
# How close a loose name must be to a title before it is taken as that product.
# Below it the shopper is offered the near misses rather than a guess.
_MATCH_RATIO = 0.72

COMPARE_DETAILS = """
query SupportCompareProducts($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product {
      legacyResourceId
      title
      handle
      productType
      tags
      status
      onlineStoreUrl
      description
      descriptionHtml
      featuredMedia { ... on MediaImage { image { url altText } } }
      options { name values }
      priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } }
      variants(first: 10) { nodes { legacyResourceId availableForSale } }
    }
  }
}
"""

_NOISE = {"the", "and", "in", "of", "for", "a", "an", "with"}
_WORD_RE = re.compile(r"[a-z0-9]+")
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_FABRIC_RE = re.compile(
    r"\b(?:\d{1,3}%\s*)?(?:organic\s+)?(?:cotton|linen|wool|merino|alpaca|cashmere|silk|leather|"
    r"suede|polyester|viscose|corduroy|denim|jersey|muslin|velvet|tweed|voile|poplin)\b",
    re.I,
)
# Case-sensitive on purpose: "made in Spain using leather" must stop at Spain.
_MADE_IN_RE = re.compile(r"\b[Mm]ade in ([A-Z][a-z]+(?: [A-Z][a-z]+)?)")
_VERSUS_RE = re.compile(r"\s+(?:vs\.?|versus)\s+", re.I)
_AUDIENCE = ("Girls", "Boys", "Baby")


def _stem(word: str) -> str:
    if word.endswith("sses"):
        return word[:-2]                    # dresses -> dress
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]                    # shoes -> shoe, but dress stays dress
    return word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in _NOISE}


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def resolve(name: str, ids: dict[str, str], titles: dict[str, str]) -> tuple[str | None, list[str]]:
    """The product a shopper's name points at, or the nearest titles if none does.

    Tried in order: the exact title; a title holding every word they used ("alice
    floral" finds the Alice Floral dress); then a close spelling. Search alone
    cannot do this - it returned nothing for "Amelia dress".
    """
    wanted = " ".join(name.lower().split())
    if not wanted:
        return None, []
    if wanted in ids:
        return ids[wanted], []
    want = _stems(wanted)
    containing = [t for t in ids if want and want <= _stems(t)]
    if containing:
        return ids[max(containing, key=lambda t: _ratio(wanted, t))], []
    ranked = sorted(ids, key=lambda t: (-len(want & _stems(t)), -_ratio(wanted, t)))
    if ranked and _ratio(wanted, ranked[0]) >= _MATCH_RATIO:
        return ids[ranked[0]], []
    return None, [titles[ids[t]] for t in ranked[:3] if want & _stems(t)]


def _first_sentence(text: str | None, limit: int = 160) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    end = text.find(". ")
    return (text[: end + 1] if 0 < end < limit else text[:limit]).strip() or None


def _highlights(description_html: str | None) -> list[str]:
    """The product's own bullet points - the nearest thing this store has to specs."""
    out: list[str] = []
    for raw in _LI_RE.findall(description_html or ""):
        text = " ".join(html.unescape(_TAG_RE.sub(" ", raw)).split())
        if text and text not in out:
            out.append(text)
    return out[:6]


_LABEL_RE = re.compile(r"^\s*(?:material|fabric|composition)s?\s*:\s*", re.I)
_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def _fabric(highlights: list[str], description: str) -> str | None:
    for bullet in highlights:
        found = _FABRIC_RE.search(bullet)
        if found:
            # A bullet reads better than the bare word - "100% Organic Cotton",
            # not "cotton" - but "Material: 100% Alpaca Wool (lanolin free and
            # hypoallergenic)" is a sentence, not a spec.
            clean = _PAREN_RE.sub("", _LABEL_RE.sub("", bullet)).strip(" .;,")
            return clean if len(clean) <= 40 else found.group(0)
    found = _FABRIC_RE.search(description or "")
    return found.group(0) if found else None


def _made_in(highlights: list[str], description: str) -> str | None:
    for text in [*highlights, description or ""]:
        found = _MADE_IN_RE.search(text)
        if found:
            return found.group(1)
    return None


def _facts(node: dict, currency: str) -> dict:
    options = {o["name"].strip().lower(): o["values"] for o in node.get("options") or []}
    colours = options.get("color") or options.get("colour") or []
    sizes = options.get("size") or []
    description = node.get("description") or ""
    highlights = _highlights(node.get("descriptionHtml"))
    variants = node["variants"]["nodes"]
    buyable = next((v for v in variants if v.get("availableForSale")), variants[0] if variants else {})
    tags = {t.lower() for t in node.get("tags") or []}
    audience = [a for a in _AUDIENCE if a.lower() in tags]
    size_range = (sizes[0] if len(sizes) == 1 else f"{sizes[0]} - {sizes[-1]}") if sizes else None
    fabric = _fabric(highlights, description)
    made_in = _made_in(highlights, description)
    specs = [
        {"label": label, "value": value}
        for label, value in (
            ("Type", node.get("productType")),
            ("For", ", ".join(audience)),
            ("Sizes", size_range),
            ("Colours", ", ".join(colours)),
            ("Fabric", fabric),
            ("Made in", made_in),
        )
        if value
    ]
    return {
        "product_id": node.get("legacyResourceId"),
        "variant_id": buyable.get("legacyResourceId"),
        "title": node["title"],
        "handle": node["handle"],
        "category": node.get("productType"),
        "price_from": float(node["priceRangeV2"]["minVariantPrice"]["amount"]),
        "price_to": float(node["priceRangeV2"]["maxVariantPrice"]["amount"]),
        "currency": currency,
        "image": product_image(node),
        "url": product_url(node),
        "for": audience,
        "colours": colours,
        "size_range": size_range,
        "fabric": fabric,
        "made_in": made_in,
        "about": _first_sentence(description),
        "highlights": highlights,
        "specs": specs,
    }


def _contrast(products: list[dict], currency: str) -> tuple[list[str], list[str]]:
    """What they share and where they differ, as plain sentences the agent can use."""
    every = "Both" if len(products) == 2 else "All"
    common: list[str] = []
    differ: list[str] = []

    def money(value: float) -> str:
        return f"{value:.2f} {currency}"

    types = {p["category"] for p in products}
    if len(types) == 1 and None not in types:
        common.append(f"{every} are {plural(next(iter(types))).lower()}")
    else:
        differ.append("Different kinds of piece: " + "; ".join(f"{p['title']} ({p['category']})" for p in products))

    by_price = sorted(products, key=lambda p: p["price_from"])
    low, high = by_price[0], by_price[-1]
    if high["price_from"] == low["price_from"]:
        common.append(f"{every} cost {money(low['price_from'])}")
    else:
        differ.append(
            f"{low['title']} is the least expensive at {money(low['price_from'])}, "
            f"{money(high['price_from'] - low['price_from'])} less than {high['title']} "
            f"at {money(high['price_from'])}"
        )

    ranges = {p["size_range"] for p in products}
    if len(ranges) == 1 and None not in ranges:
        common.append(f"{every} come in sizes {next(iter(ranges))}")
    elif any(ranges):
        differ.append("Sizes: " + "; ".join(f"{p['title']} {p['size_range'] or 'one size'}" for p in products))

    palettes = [{c.lower(): c for c in p["colours"]} for p in products]
    if all(palettes):
        shared = set.intersection(*(set(p) for p in palettes))
        if shared:
            common.append(f"{every} come in " + ", ".join(sorted(palettes[0][c] for c in shared)))
        for product, palette in zip(products, palettes):
            others = set().union(*(set(p) for p in palettes if p is not palette))
            only = [palette[c] for c in palette if c not in others]
            if only:
                differ.append(f"Only {product['title']} comes in {', '.join(only)}")

    audiences = {tuple(p["for"]) for p in products}
    if len(audiences) == 1 and () not in audiences:
        common.append(f"{every} are for {' and '.join(next(iter(audiences))).lower()}")
    elif len(audiences) > 1 and () not in audiences:
        differ.append("Who for: " + "; ".join(f"{p['title']} {'/'.join(p['for'])}" for p in products))

    for field, label in (("fabric", "Fabric"), ("made_in", "Made in")):
        values = [p[field] for p in products]
        if all(values):
            if len({v.lower() for v in values}) == 1:
                common.append(f"{label}: {values[0]} for {every.lower()}")
            else:
                differ.append(f"{label}: " + "; ".join(f"{p['title']} {p[field]}" for p in products))
    return common, differ


def _heading(products: list[dict]) -> str:
    types = list(dict.fromkeys(p["category"] for p in products if p.get("category")))
    return f"{' & '.join(types)} Comparison" if types else "Comparison"


async def compare(names: list[str] | str) -> dict:
    """Up to four named products, side by side, with what they share and how they differ."""
    raw = names if isinstance(names, list) else [names]
    if len(raw) == 1:
        raw = _VERSUS_RE.split(str(raw[0]))     # "Alice vs Catherine" as one string
    asked = [n for n in (" ".join(str(x).split()) for x in raw) if n]

    tree = await collection_tree()
    chosen: list[str] = []
    not_found: list[dict] = []
    repeated: list[str] = []
    for name in asked:
        product_id, near = resolve(name, tree["ids"], tree["titles"])
        if product_id is None:
            not_found.append({"asked_for": name, "did_you_mean": near})
        elif product_id in chosen:
            repeated.append(name)
        else:
            chosen.append(product_id)
    dropped = len(chosen) - MAX_COMPARE

    currency = (await shop_info())["currency"]
    products: list[dict] = []
    if chosen:
        data = await graphql(
            COMPARE_DETAILS,
            {"ids": [f"gid://shopify/Product/{pid}" for pid in chosen[:MAX_COMPARE]]},
        )
        products = [_facts(n, currency) for n in data["nodes"] if n and n.get("status") == "ACTIVE"]

    common, differ = _contrast(products, currency) if len(products) >= 2 else ([], [])
    result = {
        "found": len(products) >= 2,
        "currency": currency,
        "heading": _heading(products),
        "count": len(products),
        "in_common": common,
        "differences": differ,
        "not_found": not_found,
        "products": products,
    }
    notes = []
    if dropped > 0:
        notes.append(f"Only the first {MAX_COMPARE} were compared; {dropped} more were named.")
    if repeated:
        # Otherwise two names for one dress come back as found=false with nothing
        # to say why.
        notes.append(f"{', '.join(repeated)} is the same product as another name given, so it is shown once.")
    if notes:
        result["notes"] = notes
    return result
