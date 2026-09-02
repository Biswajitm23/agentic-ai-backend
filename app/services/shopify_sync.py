"""Pulls the live store into the database: products, orders, and everything the
dashboard derives from them.

The dashboard, analysis endpoints and the admin agent all read from the database,
so a sync is what makes those layers reflect the real store. The customer
support agent does not go through here - it reads Shopify live, via
``shopify_storefront``.

What comes from where (the app's scopes are read_products, read_inventory,
read_orders, read_all_orders):

* **Products** - every product and variant, paginated, with stock and unit cost.
* **Orders** - every order, paginated, with financial breakdown, line items,
  payment fees, sales channel and marketing attribution (UTM / discount code).
* **Campaigns** - Shopify's marketing activities when the app has
  ``read_marketing_events``; otherwise campaigns are derived from order
  attribution (utm_campaign, then discount code, then sales channel), each with
  its real order count and revenue. Ad spend and impressions are not in Shopify
  order data, so those stay 0 for derived campaigns.
* **Expenses** - derived per order: cost of goods sold (line quantity x unit
  cost), payment processing fees, refunds, discounts given and sales tax
  collected. Shopify has no general-ledger API, so overheads such as payroll or
  software must be entered manually (``source = "manual"`` rows are kept).
* **Tasks** - regenerated from store state: unfulfilled orders, stock-outs,
  unpaid orders, variants missing a unit cost.

Rows the store owns carry ``source = "shopify"`` and are replaced on every sync;
demo ``seed`` rows are removed the first time a real store is synced.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    SOURCE_SEED,
    SOURCE_SHOPIFY,
    Campaign,
    Expense,
    OpsTask,
    Order,
    OrderLine,
    Product,
    StoreSetting,
)
from app.services import rag
from app.services.shopify_client import ShopifyError, graphql, is_configured  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
DEFAULT_REORDER_LEVEL = 10
ACTIVE_WINDOW_DAYS = 30

SHOP_QUERY = """
{
  shop { name currencyCode ianaTimezone }
  currentAppInstallation { accessScopes { handle } }
}
"""

PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        productType
        vendor
        status
        variants(first: 100) {
          edges {
            node {
              id
              sku
              title
              price
              inventoryQuantity
              inventoryItem { unitCost { amount } }
            }
          }
        }
      }
    }
  }
}
"""

ORDERS_QUERY = """
query Orders($cursor: String) {
  orders(first: 50, after: $cursor, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        cancelledAt
        sourceName
        displayFinancialStatus
        displayFulfillmentStatus
        totalPriceSet { shopMoney { amount } }
        subtotalPriceSet { shopMoney { amount } }
        totalTaxSet { shopMoney { amount } }
        totalShippingPriceSet { shopMoney { amount } }
        totalDiscountsSet { shopMoney { amount } }
        totalRefundedSet { shopMoney { amount } }
        discountCodes
        shippingAddress { name }
        billingAddress { name }
        customerJourneySummary {
          firstVisit { source utmParameters { campaign source medium } }
          lastVisit { source utmParameters { campaign source medium } }
        }
        lineItems(first: 50) {
          edges {
            node {
              title
              quantity
              sku
              originalUnitPriceSet { shopMoney { amount } }
              variant { inventoryItem { unitCost { amount } } }
            }
          }
        }
        transactions(first: 20) {
          kind
          status
          gateway
          amountSet { shopMoney { amount } }
          fees { amount { amount } }
        }
      }
    }
  }
}
"""

MARKETING_QUERY = """
{
  marketingActivities(first: 100) {
    edges {
      node {
        id
        title
        status
        marketingChannelType
        tactic
        createdAt
        budget { total { amount } }
        utmParameters { campaign source medium }
      }
    }
  }
}
"""

# Scopes that would make more of the dashboard first-party Shopify data.
OPTIONAL_SCOPES = {
    "read_marketing_events": "campaigns come straight from Shopify Marketing instead of order attribution",
    "read_customers": "orders show the customer's name",
    "read_discounts": "discount codes appear as campaigns even before they are used",
}

CHANNEL_LABELS = {
    "web": "Online Store",
    "pos": "Point of Sale",
    "shopify_draft_order": "Draft orders",
    "draft_order": "Draft orders",
    "iphone": "Shopify mobile app",
    "android": "Shopify mobile app",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _amount(money_set: dict | None) -> float:
    try:
        return float(((money_set or {}).get("shopMoney") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _gid_tail(gid: str | None) -> str | None:
    return gid.rsplit("/", 1)[-1] if gid else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _order_status(node: dict) -> str:
    if node.get("cancelledAt"):
        return "cancelled"
    fulfillment = (node.get("displayFulfillmentStatus") or "").upper()
    if fulfillment == "FULFILLED":
        return "fulfilled"
    if fulfillment in ("IN_PROGRESS", "PARTIALLY_FULFILLED", "SCHEDULED"):
        return "processing"
    return "pending"


def _channel_label(channel: str | None) -> str:
    if not channel:
        return "Online Store"
    return CHANNEL_LABELS.get(channel, channel.replace("_", " ").title())


async def _paginate(client: httpx.AsyncClient, query: str, root: str) -> list[dict]:
    nodes: list[dict] = []
    cursor: str | None = None
    while True:
        data = await graphql(query, {"cursor": cursor}, client=client)
        page = data[root]
        nodes.extend(edge["node"] for edge in page["edges"])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


async def _set_setting(db: AsyncSession, key: str, value) -> None:
    row = await db.get(StoreSetting, key)
    if row is None:
        db.add(StoreSetting(key=key, value=value))
    else:
        row.value = value


# --------------------------------------------------------------------------- #
# products
# --------------------------------------------------------------------------- #


async def _sync_products(db: AsyncSession, nodes: list[dict]) -> int:
    existing = {p.sku: p for p in (await db.execute(select(Product))).scalars().all()}
    seen: set[str] = set()
    count = 0
    for p in nodes:
        for v_edge in p["variants"]["edges"]:
            v = v_edge["node"]
            sku = (v.get("sku") or "").strip() or f"VAR-{_gid_tail(v['id'])}"
            if sku in seen:  # Shopify allows duplicate SKUs across variants; keep the first
                sku = f"{sku}-{_gid_tail(v['id'])}"
            seen.add(sku)
            variant_title = None if v.get("title") in (None, "Default Title") else v["title"]
            unit_cost = ((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount")
            row = existing.get(sku)
            if row is None:
                row = Product(sku=sku, reorder_level=DEFAULT_REORDER_LEVEL)
                db.add(row)
                existing[sku] = row
            row.name = p["title"] if not variant_title else f"{p['title']} - {variant_title}"
            row.product_title = p["title"]
            row.variant_title = variant_title
            row.category = p.get("productType") or "Uncategorized"
            row.vendor = p.get("vendor")
            row.status = (p.get("status") or "ACTIVE").lower()
            row.price = float(v.get("price") or 0)
            row.cost = float(unit_cost) if unit_cost is not None else 0.0
            row.has_cost = unit_cost is not None
            row.stock_qty = int(v.get("inventoryQuantity") or 0)
            row.shopify_product_id = _gid_tail(p["id"])
            row.shopify_variant_id = _gid_tail(v["id"])
            row.source = SOURCE_SHOPIFY
            count += 1

    # Anything not in the store any more - or demo data - goes.
    stale = [p for sku, p in existing.items() if sku not in seen]
    for p in stale:
        await db.delete(p)
    return count


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #


def _attribution(node: dict) -> tuple[str | None, str | None, str | None]:
    journey = node.get("customerJourneySummary") or {}
    for visit_key in ("lastVisit", "firstVisit"):
        visit = journey.get(visit_key) or {}
        utm = visit.get("utmParameters") or {}
        if utm.get("campaign") or utm.get("source"):
            return utm.get("campaign"), utm.get("source") or visit.get("source"), utm.get("medium")
    return None, None, None


async def _sync_orders(db: AsyncSession, nodes: list[dict]) -> int:
    existing = {o.order_number: o for o in (await db.execute(select(Order))).scalars().all()}
    seen: set[str] = set()
    count = 0
    for o in nodes:
        number = o["name"]
        seen.add(number)
        row = existing.get(number)
        is_new = row is None
        if is_new:
            row = Order(order_number=number)
            existing[number] = row
        else:
            await db.execute(delete(OrderLine).where(OrderLine.order_id == row.id))

        ship = (o.get("shippingAddress") or {}).get("name")
        bill = (o.get("billingAddress") or {}).get("name")
        row.customer_name = ship or bill or ("Walk-in customer" if o.get("sourceName") == "pos" else "Guest")
        row.created_at = _parse_dt(o.get("createdAt")) or row.created_at or datetime.utcnow()
        row.status = _order_status(o)
        row.financial_status = (o.get("displayFinancialStatus") or "").lower() or None
        row.channel = o.get("sourceName") or "web"
        row.total = _amount(o.get("totalPriceSet"))
        row.subtotal = _amount(o.get("subtotalPriceSet"))
        row.tax = _amount(o.get("totalTaxSet"))
        row.shipping = _amount(o.get("totalShippingPriceSet"))
        row.discounts = _amount(o.get("totalDiscountsSet"))
        row.refunded = _amount(o.get("totalRefundedSet"))
        codes = o.get("discountCodes") or []
        row.discount_code = codes[0] if codes else None
        row.utm_campaign, row.utm_source, row.utm_medium = _attribution(o)
        row.source = SOURCE_SHOPIFY

        fees = 0.0
        for tx in o.get("transactions") or []:
            if (tx.get("status") or "").upper() != "SUCCESS":
                continue
            for fee in tx.get("fees") or []:
                fees += float(((fee.get("amount") or {}).get("amount")) or 0)
        row.payment_fees = round(fees, 2)
        if is_new:
            db.add(row)
        await db.flush()  # need row.id for the lines

        cogs = 0.0
        items = 0
        for li_edge in (o.get("lineItems") or {}).get("edges", []):
            li = li_edge["node"]
            qty = int(li.get("quantity") or 0)
            unit_cost = (((li.get("variant") or {}).get("inventoryItem") or {}).get("unitCost") or {}).get("amount")
            unit_cost_f = float(unit_cost) if unit_cost is not None else None
            db.add(
                OrderLine(
                    order_id=row.id,
                    sku=li.get("sku") or None,
                    title=li.get("title") or "Item",
                    quantity=qty,
                    unit_price=_amount(li.get("originalUnitPriceSet")),
                    unit_cost=unit_cost_f,
                )
            )
            items += qty
            if unit_cost_f is not None:
                cogs += unit_cost_f * qty
        row.cogs = round(cogs, 2)
        row.item_count = items
        count += 1

    for number, o in existing.items():
        if number not in seen:
            await db.execute(delete(OrderLine).where(OrderLine.order_id == o.id))
            await db.delete(o)
    return count


# --------------------------------------------------------------------------- #
# campaigns
# --------------------------------------------------------------------------- #


async def _fetch_marketing_activities(client: httpx.AsyncClient) -> list[dict] | None:
    """Shopify Marketing activities, or None when the app lacks the scope."""
    try:
        data = await graphql(MARKETING_QUERY, client=client)
    except ShopifyError as exc:
        if "access denied" in str(exc).lower() or "read_marketing_events" in str(exc):
            return None
        raise
    return [edge["node"] for edge in data["marketingActivities"]["edges"]]


def _campaign_status(last_order_at: datetime | None) -> str:
    if last_order_at and last_order_at >= datetime.utcnow() - timedelta(days=ACTIVE_WINDOW_DAYS):
        return "active"
    return "inactive"


async def _rebuild_campaigns(db: AsyncSession, orders: list[Order], activities: list[dict] | None) -> str:
    await db.execute(delete(Campaign).where(Campaign.source.in_([SOURCE_SHOPIFY, SOURCE_SEED])))
    live = [o for o in orders if o.status != "cancelled"]

    groups: dict[tuple[str, str], dict] = {}

    def add(key: tuple[str, str], name: str, platform: str, order: Order) -> None:
        g = groups.setdefault(
            key,
            {"name": name, "platform": platform, "orders": 0, "revenue": 0.0, "first": None, "last": None},
        )
        g["orders"] += 1
        g["revenue"] += order.total - order.refunded
        g["first"] = min(g["first"], order.created_at) if g["first"] else order.created_at
        g["last"] = max(g["last"], order.created_at) if g["last"] else order.created_at

    if activities is not None:
        # First-party campaigns; attribute orders to them by utm_campaign.
        by_utm = {
            (a.get("utmParameters") or {}).get("campaign"): a for a in activities if (a.get("utmParameters") or {}).get("campaign")
        }
        matched: set[str] = set()
        for a in activities:
            utm = (a.get("utmParameters") or {}).get("campaign")
            key = ("marketing_activity", _gid_tail(a["id"]) or a["title"])
            groups[key] = {
                "name": a["title"],
                "platform": (a.get("marketingChannelType") or "Shopify Marketing").replace("_", " ").title(),
                "orders": 0,
                "revenue": 0.0,
                "first": _parse_dt(a.get("createdAt")),
                "last": None,
                "budget": float(((a.get("budget") or {}).get("total") or {}).get("amount") or 0),
                "status_override": (a.get("status") or "").lower() or None,
            }
            if utm:
                for o in live:
                    if o.utm_campaign == utm:
                        add(key, a["title"], groups[key]["platform"], o)
                        matched.add(o.order_number)
        for o in live:
            if o.order_number not in matched:
                if o.utm_campaign and o.utm_campaign not in by_utm:
                    add(("utm_campaign", o.utm_campaign), f"UTM: {o.utm_campaign}", o.utm_source or "Web", o)
                elif o.discount_code:
                    add(("discount_code", o.discount_code), f"Code: {o.discount_code}", "Promo code", o)
                else:
                    add(("channel", o.channel or "web"), f"{_channel_label(o.channel)} - direct", "Shopify", o)
        campaign_source = "shopify_marketing"
    else:
        for o in live:
            if o.utm_campaign:
                add(("utm_campaign", o.utm_campaign), f"UTM: {o.utm_campaign}", (o.utm_source or "Web").title(), o)
            elif o.discount_code:
                add(("discount_code", o.discount_code), f"Code: {o.discount_code}", "Promo code", o)
            else:
                add(("channel", o.channel or "web"), f"{_channel_label(o.channel)} - direct", "Shopify", o)
        campaign_source = "order_attribution"

    for (attribution, key), g in groups.items():
        db.add(
            Campaign(
                name=g["name"],
                platform=g["platform"],
                status=g.get("status_override") or _campaign_status(g["last"]),
                budget=round(g.get("budget", 0.0), 2),
                spend=0.0,
                impressions=0,
                clicks=0,
                conversions=g["orders"],
                revenue=round(g["revenue"], 2),
                attribution=attribution,
                attribution_key=str(key)[:200],
                first_order_at=g["first"],
                last_order_at=g["last"],
                source=SOURCE_SHOPIFY,
            )
        )
    return campaign_source


# --------------------------------------------------------------------------- #
# expenses and tasks
# --------------------------------------------------------------------------- #


async def _rebuild_expenses(db: AsyncSession, orders: list[Order]) -> int:
    await db.execute(delete(Expense).where(Expense.source.in_([SOURCE_SHOPIFY, SOURCE_SEED])))
    count = 0
    for o in orders:
        if o.status == "cancelled":
            continue
        when = (o.created_at or datetime.utcnow()).date()
        parts = [
            ("Cost of goods sold", f"Landed cost of {o.item_count} item(s) on {o.order_number}", o.cogs),
            ("Payment processing", f"Gateway fees on {o.order_number}", o.payment_fees),
            ("Refunds", f"Refunded to customer on {o.order_number}", o.refunded),
            ("Discounts", f"Discount given on {o.order_number}" + (f" ({o.discount_code})" if o.discount_code else ""), o.discounts),
            ("Sales tax (to remit)", f"Tax collected on {o.order_number}", o.tax),
        ]
        for category, description, amount in parts:
            if amount and amount > 0:
                db.add(
                    Expense(
                        category=category,
                        description=description,
                        amount=round(amount, 2),
                        expense_date=when,
                        order_number=o.order_number,
                        source=SOURCE_SHOPIFY,
                    )
                )
                count += 1
    return count


async def _rebuild_tasks(db: AsyncSession, products: list[Product], orders: list[Order], currency: str) -> int:
    await db.execute(delete(OpsTask).where(OpsTask.source.in_([SOURCE_SHOPIFY, SOURCE_SEED])))
    today = date.today()
    tasks: list[OpsTask] = []

    open_orders = [o for o in orders if o.status in ("pending", "processing")]
    if open_orders:
        value = sum(o.total for o in open_orders)
        tasks.append(
            OpsTask(
                title=f"Fulfil {len(open_orders)} open order(s) worth {value:,.2f} {currency}",
                priority="high",
                status="open",
                due_date=today + timedelta(days=1),
                domain="operations",
                source=SOURCE_SHOPIFY,
            )
        )
    unpaid = [o for o in orders if o.status != "cancelled" and (o.financial_status or "") in ("pending", "authorized", "partially_paid")]
    if unpaid:
        tasks.append(
            OpsTask(
                title=f"Collect payment on {len(unpaid)} unpaid order(s)",
                priority="medium",
                status="open",
                due_date=today + timedelta(days=3),
                domain="finance",
                source=SOURCE_SHOPIFY,
            )
        )
    active = [p for p in products if p.status == "active"]
    out = [p for p in active if p.stock_qty <= 0]
    low = [p for p in active if 0 < p.stock_qty <= p.reorder_level]
    if out:
        tasks.append(
            OpsTask(
                title=f"Restock {len(out)} variant(s) that are out of stock",
                priority="high",
                status="open",
                due_date=today + timedelta(days=2),
                domain="inventory",
                source=SOURCE_SHOPIFY,
            )
        )
    if low:
        tasks.append(
            OpsTask(
                title=f"Reorder {len(low)} variant(s) at or below reorder level",
                priority="medium",
                status="open",
                due_date=today + timedelta(days=7),
                domain="inventory",
                source=SOURCE_SHOPIFY,
            )
        )
    no_cost = [p for p in active if not p.has_cost]
    if no_cost:
        tasks.append(
            OpsTask(
                title=f"Add unit cost in Shopify for {len(no_cost)} variant(s) so margins are accurate",
                priority="low",
                status="open",
                due_date=today + timedelta(days=14),
                domain="finance",
                source=SOURCE_SHOPIFY,
            )
        )
    live = [o for o in orders if o.status != "cancelled"]
    unattributed = [o for o in live if not o.utm_campaign and not o.discount_code]
    if live and len(unattributed) / len(live) > 0.5:
        tasks.append(
            OpsTask(
                title=f"Tag campaign links with UTM parameters - {len(unattributed)} of {len(live)} orders have no campaign attribution",
                priority="low",
                status="open",
                due_date=today + timedelta(days=14),
                domain="marketing",
                source=SOURCE_SHOPIFY,
            )
        )
    db.add_all(tasks)
    return len(tasks)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


async def sync_shopify(db: AsyncSession) -> dict:
    """Refresh every Shopify-owned row and the agent's retrieval index. Returns counts."""
    if not is_configured():
        raise RuntimeError("Shopify is not configured: set SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN")

    async with httpx.AsyncClient() as client:
        shop_data = await graphql(SHOP_QUERY, client=client)
        product_nodes = await _paginate(client, PRODUCTS_QUERY, "products")
        order_nodes = await _paginate(client, ORDERS_QUERY, "orders")
        scopes = sorted(s["handle"] for s in shop_data["currentAppInstallation"]["accessScopes"])
        activities = await _fetch_marketing_activities(client) if "read_marketing_events" in scopes else None

    shop = shop_data["shop"]
    currency = shop.get("currencyCode") or "USD"

    product_count = await _sync_products(db, product_nodes)
    order_count = await _sync_orders(db, order_nodes)
    await db.flush()

    products = (await db.execute(select(Product))).scalars().all()
    orders = (await db.execute(select(Order))).scalars().all()
    campaign_source = await _rebuild_campaigns(db, orders, activities)
    expense_count = await _rebuild_expenses(db, orders)
    task_count = await _rebuild_tasks(db, products, orders, currency)

    missing = {s: why for s, why in OPTIONAL_SCOPES.items() if s not in scopes}
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    await _set_setting(db, "shop_name", shop.get("name"))
    await _set_setting(db, "currency", currency)
    await _set_setting(db, "timezone", shop.get("ianaTimezone"))
    await _set_setting(db, "scopes", scopes)
    await _set_setting(db, "missing_scopes", missing)
    await _set_setting(db, "campaign_source", campaign_source)
    await _set_setting(db, "last_synced_at", now)
    await db.commit()

    try:
        indexed = await rag.rebuild_store_knowledge(db, currency)
    except Exception:
        logger.exception("Rebuilding the retrieval index failed; the agent will use stale chunks")
        indexed = 0

    return {
        "products_synced": product_count,
        "orders_synced": order_count,
        "campaigns_synced": len((await db.execute(select(Campaign))).scalars().all()),
        "expenses_synced": expense_count,
        "tasks_synced": task_count,
        "knowledge_indexed": indexed,
        "campaign_source": campaign_source,
        "currency": currency,
        "last_synced_at": now,
    }
