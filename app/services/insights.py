"""Domain analytics shared by the dashboard endpoints and the admin agent tools.

Everything here reads the database only; a Shopify sync is what puts the store's
real numbers there. Money is in the store's currency (see ``store_meta``).
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Expense, OpsTask, Order, Product, StoreSetting

DOMAINS = ("inventory", "marketing", "operations", "finance")
RECENT_DAYS = 30

# Priority-action severities, most urgent first.
SEVERITIES = ("critical", "high", "medium", "low")


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 2) if whole else 0.0


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# store facts
# --------------------------------------------------------------------------- #


async def store_meta(db: AsyncSession) -> dict:
    rows = (await db.execute(select(StoreSetting))).scalars().all()
    meta = {r.key: r.value for r in rows}
    return {
        "shop_name": meta.get("shop_name"),
        "currency": meta.get("currency") or "USD",
        "timezone": meta.get("timezone"),
        "last_synced_at": meta.get("last_synced_at"),
        "campaign_source": meta.get("campaign_source") or "demo",
        "scopes": meta.get("scopes") or [],
        "missing_scopes": meta.get("missing_scopes") or {},
        "storefront_url": meta.get("storefront_url"),
        "admin_url": meta.get("admin_url"),
    }


def product_links(p: Product, storefront: str | None, admin: str | None) -> dict:
    """Where a product can be opened: the shop page for live products, the
    admin page otherwise (drafts have no public page)."""
    url = f"{storefront}/products/{p.handle}" if storefront and p.handle and p.status == "active" else None
    admin_url = f"{admin}/products/{p.shopify_product_id}" if admin and p.shopify_product_id else None
    return {"url": url, "admin_url": admin_url}


async def currency(db: AsyncSession) -> str:
    row = await db.get(StoreSetting, "currency")
    return (row.value if row and isinstance(row.value, str) else None) or "USD"


# --------------------------------------------------------------------------- #
# domain summaries
# --------------------------------------------------------------------------- #


async def inventory_summary(db: AsyncSession) -> dict:
    meta = await store_meta(db)
    products = (await db.execute(select(Product).order_by(Product.stock_qty, Product.name))).scalars().all()
    active = [p for p in products if p.status == "active"]
    out_of_stock = [p for p in active if p.stock_qty <= 0]
    low_stock = [p for p in active if p.stock_qty <= p.reorder_level]
    missing_cost = [p for p in active if not p.has_cost]
    retail_value = sum(p.stock_qty * p.price for p in active if p.stock_qty > 0)
    categories: dict[str, dict] = {}
    for p in active:
        c = categories.setdefault(p.category, {"skus": 0, "units": 0, "low_stock": 0})
        c["skus"] += 1
        c["units"] += max(p.stock_qty, 0)
        c["low_stock"] += 1 if p.stock_qty <= p.reorder_level else 0
    return {
        "currency": meta["currency"],
        "storefront_url": meta["storefront_url"],
        "admin_url": meta["admin_url"],
        "total_skus": len(active),
        "total_products": len({p.shopify_product_id or p.sku for p in active}),
        "total_units": sum(max(p.stock_qty, 0) for p in active),
        "stock_value": round(sum(max(p.stock_qty, 0) * p.cost for p in active), 2),
        "retail_value": round(retail_value, 2),
        "low_stock_count": len(low_stock),
        "out_of_stock_count": len(out_of_stock),
        "missing_cost_count": len(missing_cost),
        "draft_or_archived": len(products) - len(active),
        "categories": [{"category": k, **v} for k, v in sorted(categories.items(), key=lambda kv: -kv[1]["units"])],
        "low_stock_items": [
            {
                "sku": p.sku,
                "name": p.name,
                "stock_qty": p.stock_qty,
                "reorder_level": p.reorder_level,
                **product_links(p, meta["storefront_url"], meta["admin_url"]),
            }
            for p in low_stock
        ],
        "products": [
            {
                "sku": p.sku,
                "name": p.name,
                "category": p.category,
                "vendor": p.vendor,
                "status": p.status,
                "price": p.price,
                "cost": p.cost if p.has_cost else None,
                "stock_qty": p.stock_qty,
                "reorder_level": p.reorder_level,
                "low_stock": p.stock_qty <= p.reorder_level,
                "out_of_stock": p.stock_qty <= 0,
                **product_links(p, meta["storefront_url"], meta["admin_url"]),
            }
            for p in products
        ],
    }


async def marketing_summary(db: AsyncSession) -> dict:
    campaigns = (await db.execute(select(Campaign).order_by(Campaign.revenue.desc()))).scalars().all()
    meta = await store_meta(db)
    spend = sum(c.spend for c in campaigns)
    revenue = sum(c.revenue for c in campaigns)
    clicks = sum(c.clicks for c in campaigns)
    impressions = sum(c.impressions for c in campaigns)
    conversions = sum(c.conversions for c in campaigns)
    orders = (await db.execute(select(Order).where(Order.status != "cancelled"))).scalars().all()
    attributed = [o for o in orders if o.utm_campaign or o.discount_code]
    return {
        "currency": meta["currency"],
        "campaign_source": meta["campaign_source"],
        "total_campaigns": len(campaigns),
        "active_campaigns": sum(1 for c in campaigns if c.status == "active"),
        "total_spend": round(spend, 2),
        "total_budget": round(sum(c.budget for c in campaigns), 2),
        "total_revenue": round(revenue, 2),
        "total_conversions": conversions,
        "roas": round(revenue / spend, 2) if spend else None,
        "ctr_pct": _pct(clicks, impressions) if impressions else None,
        "avg_order_value": round(revenue / conversions, 2) if conversions else 0,
        "attributed_orders": len(attributed),
        "attribution_rate_pct": _pct(len(attributed), len(orders)),
        "campaigns": [
            {
                "name": c.name,
                "platform": c.platform,
                "status": c.status,
                "budget": c.budget,
                "spend": c.spend,
                "impressions": c.impressions,
                "clicks": c.clicks,
                "conversions": c.conversions,
                "revenue": c.revenue,
                "roas": round(c.revenue / c.spend, 2) if c.spend else None,
                "attribution": c.attribution,
                "last_order_at": c.last_order_at.isoformat() if c.last_order_at else None,
            }
            for c in campaigns
        ],
    }


async def operations_summary(db: AsyncSession) -> dict:
    meta = await store_meta(db)
    admin = meta["admin_url"]
    orders = (await db.execute(select(Order).order_by(Order.created_at.desc(), Order.id.desc()))).scalars().all()
    tasks = (await db.execute(select(OpsTask).order_by(OpsTask.due_date))).scalars().all()
    today = date.today()
    by_status: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    for o in orders:
        by_status[o.status] = by_status.get(o.status, 0) + 1
        by_channel[o.channel or "web"] = by_channel.get(o.channel or "web", 0) + 1
    live = [o for o in orders if o.status != "cancelled"]
    open_orders = [o for o in live if o.status in ("pending", "processing")]
    recent_cutoff = datetime.utcnow() - timedelta(days=RECENT_DAYS)
    recent = [o for o in live if o.created_at and o.created_at >= recent_cutoff]
    overdue = [t for t in tasks if t.status != "done" and t.due_date and t.due_date < today]
    return {
        "currency": meta["currency"],
        "admin_url": admin,
        "total_orders": len(orders),
        "orders_by_status": by_status,
        "orders_by_channel": by_channel,
        "pending_orders": len(open_orders),
        "pending_value": round(sum(o.total for o in open_orders), 2),
        "fulfillment_rate_pct": _pct(by_status.get("fulfilled", 0), len(live)),
        "cancellation_rate_pct": _pct(by_status.get("cancelled", 0), len(orders)),
        "orders_last_30d": len(recent),
        "revenue_last_30d": round(sum(o.total - o.refunded for o in recent), 2),
        "avg_order_value": round(sum(o.total for o in live) / len(live), 2) if live else 0,
        "open_tasks": sum(1 for t in tasks if t.status != "done"),
        "high_priority_tasks": sum(1 for t in tasks if t.priority == "high" and t.status != "done"),
        "overdue_tasks": len(overdue),
        "tasks": [
            {
                "title": t.title,
                "priority": t.priority,
                "status": t.status,
                "domain": t.domain,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "overdue": t.status != "done" and bool(t.due_date and t.due_date < today),
            }
            for t in tasks
        ],
        "recent_orders": [
            {
                "order_number": o.order_number,
                "customer_name": o.customer_name,
                "total": o.total,
                "status": o.status,
                "financial_status": o.financial_status,
                "channel": o.channel,
                "items": o.item_count,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "admin_url": f"{admin}/orders/{o.shopify_order_id}" if admin and o.shopify_order_id else None,
            }
            for o in orders[:15]
        ],
    }


async def finance_summary(db: AsyncSession) -> dict:
    orders = (await db.execute(select(Order).where(Order.status != "cancelled"))).scalars().all()
    expenses = (await db.execute(select(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc()))).scalars().all()
    gross = sum(o.total for o in orders)
    refunds = sum(o.refunded for o in orders)
    tax = sum(o.tax for o in orders)
    shipping = sum(o.shipping for o in orders)
    discounts = sum(o.discounts for o in orders)
    cogs = sum(o.cogs for o in orders)
    total_revenue = round(gross - refunds, 2)
    net_sales = round(gross - refunds - tax - shipping, 2)
    total_expenses = round(sum(e.amount for e in expenses), 2)
    net_profit = round(total_revenue - total_expenses, 2)
    gross_profit = round(net_sales - cogs, 2)
    by_category: dict[str, float] = {}
    for e in expenses:
        by_category[e.category] = round(by_category.get(e.category, 0) + e.amount, 2)
    unpaid = [o for o in orders if (o.financial_status or "") in ("pending", "authorized", "partially_paid")]
    monthly: dict[str, dict] = {}
    for o in orders:
        if not o.created_at:
            continue
        key = o.created_at.strftime("%Y-%m")
        m = monthly.setdefault(key, {"month": key, "revenue": 0.0, "orders": 0})
        m["revenue"] = round(m["revenue"] + o.total - o.refunded, 2)
        m["orders"] += 1
    for e in expenses:
        key = e.expense_date.strftime("%Y-%m")
        m = monthly.setdefault(key, {"month": key, "revenue": 0.0, "orders": 0})
        m["expenses"] = round(m.get("expenses", 0.0) + e.amount, 2)
    return {
        "currency": await currency(db),
        "total_revenue": total_revenue,
        "gross_sales": round(gross, 2),
        "net_sales": net_sales,
        "refunds": round(refunds, 2),
        "tax_collected": round(tax, 2),
        "shipping_collected": round(shipping, 2),
        "discounts_given": round(discounts, 2),
        "cogs": round(cogs, 2),
        "gross_profit": gross_profit,
        "gross_margin_pct": _pct(gross_profit, net_sales),
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "profit_margin_pct": _pct(net_profit, total_revenue),
        "unpaid_orders": len(unpaid),
        "unpaid_value": round(sum(o.total for o in unpaid), 2),
        "expenses_by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "monthly": sorted(monthly.values(), key=lambda m: m["month"]),
        "expenses": [
            {
                "category": e.category,
                "description": e.description,
                "amount": e.amount,
                "date": e.expense_date.isoformat(),
                "order_number": e.order_number,
                "source": e.source,
            }
            for e in expenses[:50]
        ],
    }


SUMMARY_FUNCS = {
    "inventory": inventory_summary,
    "marketing": marketing_summary,
    "operations": operations_summary,
    "finance": finance_summary,
}


async def all_summaries(db: AsyncSession) -> dict[str, dict]:
    return {domain: await func(db) for domain, func in SUMMARY_FUNCS.items()}


# --------------------------------------------------------------------------- #
# priority actions
# --------------------------------------------------------------------------- #


def _action(
    domain: str,
    severity: str,
    title: str,
    detail: str,
    metric: str,
    next_step: str,
    impact: str = "",
    steps: list[str] | None = None,
) -> dict:
    return {
        "domain": domain,
        "severity": severity,
        "title": title,
        "detail": detail,
        "metric": metric,
        "next_step": next_step,
        "impact": impact,
        "steps": steps or [],
        "link": f"#{domain}-analysis",
    }


def derive_priority_actions(s: dict[str, dict]) -> list[dict]:
    """Rule-based, explainable actions from the four domain summaries.

    Each action carries what is wrong (detail), why it matters (impact), the
    number behind it (metric) and how to fix it (steps + next_step), so the
    dashboard can show a one-line row that opens into a working checklist.
    """
    inv, mkt, ops, fin = s["inventory"], s["marketing"], s["operations"], s["finance"]
    cur = fin["currency"]
    actions: list[dict] = []

    # ---------------------------------------------------------------- inventory
    if inv["out_of_stock_count"]:
        out_items = [i for i in inv["low_stock_items"] if i["stock_qty"] <= 0]
        names = ", ".join(i["name"] for i in out_items[:6])
        actions.append(
            _action(
                "inventory",
                "critical",
                f"{inv['out_of_stock_count']} variant(s) are out of stock",
                f"Shoppers can see but cannot buy: {names}{'…' if len(out_items) > 6 else ''}.",
                f"{inv['out_of_stock_count']} of {inv['total_skus']} SKUs",
                "Raise a purchase order today, or hide the variants until stock lands.",
                impact="Every visit to an out-of-stock page is a lost sale and a signal to search engines that the listing is stale.",
                steps=[
                    "Open Inventory analysis and filter to 'Needs attention' to see the exact SKUs.",
                    "Check incoming purchase orders in Shopify Admin → Products → Inventory → Incoming.",
                    "For anything with no inbound stock, either reorder now or set the variant to 'Continue selling when out of stock' off and hide it.",
                    "Add a back-in-stock notification so the demand is captured, not lost.",
                ],
            )
        )
    low_only = inv["low_stock_count"] - inv["out_of_stock_count"]
    if low_only > 0:
        low_items = [i for i in inv["low_stock_items"] if i["stock_qty"] > 0]
        actions.append(
            _action(
                "inventory",
                "high",
                f"{low_only} variant(s) at or below reorder level",
                "These will sell through before a normal supplier lead time: "
                + ", ".join(f"{i['name']} ({i['stock_qty']} left)" for i in low_items[:5])
                + ("…" if len(low_items) > 5 else "")
                + ".",
                f"{low_only} SKUs",
                "Reorder this week, prioritising the fastest sellers.",
                impact="Running out mid-campaign wastes the marketing spend that drove the traffic.",
                steps=[
                    "Sort Inventory analysis by stock to see which variants are closest to zero.",
                    "Compare each against its last-30-day sales to size the reorder.",
                    "Send purchase orders to suppliers; note expected arrival dates in Shopify.",
                    "Raise the reorder level on anything that keeps triggering this alert.",
                ],
            )
        )
    if inv["missing_cost_count"]:
        actions.append(
            _action(
                "inventory",
                "medium",
                f"{inv['missing_cost_count']} variant(s) have no unit cost",
                "Without a cost per item Shopify cannot tell us what stock is worth or what each order really earns.",
                f"{inv['missing_cost_count']} of {inv['total_skus']} SKUs",
                "Enter 'Cost per item' on each variant in Shopify.",
                impact="Stock value, cost of goods, gross margin and the Finance health score are all understated until this is fixed.",
                steps=[
                    "In Shopify Admin go to Products → Inventory and export a CSV.",
                    "Fill the 'Cost per item' column from supplier invoices.",
                    "Re-import the CSV, then press 'Sync store data' here.",
                    "Check the Finance section: gross margin should now reflect real cost of goods.",
                ],
            )
        )
    if inv["total_skus"] == 0:
        actions.append(
            _action(
                "inventory",
                "high",
                "No active products",
                "The catalogue is empty or every product is a draft.",
                "0 SKUs",
                "Publish products or sync the store.",
                impact="Nothing can be sold until at least one product is active.",
                steps=["Open Shopify Admin → Products and set products to Active.", "Press 'Sync store data'."],
            )
        )

    # ---------------------------------------------------------------- marketing
    overspent = [c for c in mkt["campaigns"] if c["budget"] and c["spend"] > c["budget"]]
    if overspent:
        actions.append(
            _action(
                "marketing",
                "high",
                f"{len(overspent)} campaign(s) over budget",
                ", ".join(f"{c['name']} ({c['spend']:,.0f} of {c['budget']:,.0f} {cur})" for c in overspent[:4]),
                f"{len(overspent)} campaigns",
                "Cap daily spend or raise the budget deliberately.",
                impact="Uncapped spend erodes the margin the campaign was supposed to create.",
                steps=[
                    "Open each campaign in its ad platform and set a daily cap.",
                    "Compare its ROAS with the others in Marketing analysis; move budget to the best performer.",
                ],
            )
        )
    weak = [c for c in mkt["campaigns"] if c["roas"] is not None and c["roas"] < 1 and c["spend"] > 0]
    if weak:
        actions.append(
            _action(
                "marketing",
                "high",
                f"{len(weak)} campaign(s) return less than they cost",
                ", ".join(f"{c['name']} ({c['roas']}x)" for c in weak[:4]),
                "ROAS < 1.0x",
                "Pause them, or rework creative and targeting before spending more.",
                impact="Each unit of spend on these campaigns loses money before any other cost is counted.",
                steps=[
                    "Pause the campaign in the ad platform.",
                    "Check landing-page conversion and audience overlap before relaunching.",
                    "Relaunch with a small test budget and a clear ROAS target (2x or better).",
                ],
            )
        )
    if mkt["total_campaigns"] and mkt["active_campaigns"] == 0:
        actions.append(
            _action(
                "marketing",
                "medium",
                "No channel has produced an order in 30 days",
                "Every campaign or sales channel is inactive on a 30-day window.",
                f"0 of {mkt['total_campaigns']} active",
                "Run a promotion or re-engage past customers this week.",
                impact="Without fresh demand the store is coasting on organic traffic only.",
                steps=[
                    "Pick the channel with the highest historic revenue in Marketing analysis.",
                    "Launch a time-boxed offer (a discount code makes it measurable here).",
                    "Email past customers; Shopify Email is free for the first 10,000 sends a month.",
                ],
            )
        )
    if mkt["campaign_source"] == "order_attribution" and mkt["attribution_rate_pct"] < 50:
        actions.append(
            _action(
                "marketing",
                "medium",
                f"Only {mkt['attribution_rate_pct']:.0f}% of orders are attributed to a campaign",
                "Most orders carry no UTM tag or discount code, so marketing results cannot be separated from direct sales.",
                f"{mkt['attributed_orders']} attributed orders",
                "Tag every campaign link with utm_campaign and give each promotion its own discount code.",
                impact="Until attribution improves, ROAS and channel comparisons here are estimates at best.",
                steps=[
                    "Add utm_source, utm_medium and utm_campaign to every ad, email and social link.",
                    "Create one discount code per promotion so orders can be tied back to it.",
                    "Grant the app the read_marketing_events scope so Shopify Marketing campaigns sync directly.",
                ],
            )
        )
    if mkt["campaigns"]:
        top = mkt["campaigns"][0]
        if top["revenue"] > 0:
            actions.append(
                _action(
                    "marketing",
                    "low",
                    f"Top channel: {top['name']}",
                    f"{top['conversions']} order(s), {top['revenue']:,.2f} {cur} in revenue.",
                    f"{_pct(top['revenue'], mkt['total_revenue']):.0f}% of attributed revenue",
                    "Double down on what is already converting.",
                    impact="Scaling a proven channel is cheaper than opening a new one.",
                    steps=[
                        "Increase budget or posting frequency on this channel by 20% and watch ROAS for two weeks.",
                        "Copy its best-performing creative and offer to the second-best channel.",
                    ],
                )
            )

    # --------------------------------------------------------------- operations
    if ops["pending_orders"]:
        actions.append(
            _action(
                "operations",
                "high" if ops["pending_orders"] >= 3 else "medium",
                f"{ops['pending_orders']} order(s) awaiting fulfilment",
                f"{ops['pending_value']:,.2f} {cur} of paid demand has not shipped yet.",
                f"{ops['fulfillment_rate_pct']:.0f}% fulfilled",
                "Pick, pack and mark them fulfilled in Shopify today.",
                impact="Late shipping is the top driver of refund requests and one-star reviews.",
                steps=[
                    "Open Operations analysis; the oldest pending orders are at the top.",
                    "In Shopify Admin → Orders filter by 'Unfulfilled' and print packing slips.",
                    "Mark each order fulfilled with a tracking number so the customer is notified.",
                    "If stock is the blocker, tell the customer and offer a substitute or refund.",
                ],
            )
        )
    if ops["overdue_tasks"]:
        actions.append(
            _action(
                "operations",
                "high",
                f"{ops['overdue_tasks']} task(s) are overdue",
                ", ".join(t["title"] for t in ops["tasks"] if t["overdue"])[:220],
                f"{ops['overdue_tasks']} overdue",
                "Reassign or close them out today.",
                impact="Overdue tasks usually hide a stalled order, restock or payment.",
                steps=["Review each task in Operations analysis.", "Close what is done; give the rest a new owner and date."],
            )
        )
    elif ops["high_priority_tasks"]:
        actions.append(
            _action(
                "operations",
                "medium",
                f"{ops['high_priority_tasks']} high-priority task(s) open",
                ", ".join(t["title"] for t in ops["tasks"] if t["priority"] == "high" and t["status"] != "done")[:220],
                f"{ops['open_tasks']} open in total",
                "Work these before anything else today.",
                impact="High-priority tasks are generated from live store problems, so they age badly.",
                steps=["Open Operations analysis → Tasks.", "Start with the earliest due date."],
            )
        )
    if ops["cancellation_rate_pct"] > 10:
        actions.append(
            _action(
                "operations",
                "medium",
                f"Cancellation rate is {ops['cancellation_rate_pct']:.0f}%",
                "More than one order in ten is cancelled.",
                f"{ops['orders_by_status'].get('cancelled', 0)} cancelled",
                "Find the cause: stock-outs, payment failures or fraud.",
                impact="Cancelled orders cost the acquisition spend that won them and skew every conversion metric.",
                steps=[
                    "In Shopify Admin filter orders by 'Cancelled' and read the cancellation reasons.",
                    "If stock-related, fix inventory first; if payment-related, review the gateway's decline codes.",
                ],
            )
        )
    if ops["orders_last_30d"] == 0 and ops["total_orders"]:
        actions.append(
            _action(
                "operations",
                "medium",
                "No orders in the last 30 days",
                "The store has gone quiet.",
                "0 orders / 30d",
                "Check the storefront works end to end, then run a promotion.",
                impact="A month without orders usually means a broken checkout or no traffic, both fixable.",
                steps=[
                    "Place a test order on the storefront to confirm checkout works.",
                    "Check Shopify Analytics for sessions; if traffic exists, the problem is conversion.",
                    "Launch an offer to past customers.",
                ],
            )
        )

    # ------------------------------------------------------------------ finance
    if fin["net_profit"] < 0:
        actions.append(
            _action(
                "finance",
                "critical",
                "The store is running at a loss",
                f"Expenses of {fin['total_expenses']:,.2f} {cur} exceed revenue of {fin['total_revenue']:,.2f} {cur}.",
                f"{fin['profit_margin_pct']:.0f}% margin",
                "Cut the largest cost category or raise prices on low-margin items.",
                impact="Losses compound: cash runs out before the fix has time to work.",
                steps=[
                    "Open Finance analysis → Expenses by category and start with the largest.",
                    "List products by gross margin; raise prices or drop the lowest.",
                    "Set a weekly check on net profit until it turns positive.",
                ],
            )
        )
    elif fin["total_revenue"] and fin["profit_margin_pct"] < 15:
        actions.append(
            _action(
                "finance",
                "high",
                f"Thin net margin: {fin['profit_margin_pct']:.0f}%",
                "Below a 15% net margin a small cost increase turns into a loss.",
                f"{fin['net_profit']:,.2f} {cur} net",
                "Review pricing and the two biggest expense categories.",
                impact="A thin margin leaves no room for a refund wave or a shipping-rate increase.",
                steps=["Compare price against unit cost per product.", "Negotiate the top expense category first."],
            )
        )
    if fin["unpaid_orders"]:
        actions.append(
            _action(
                "finance",
                "medium",
                f"{fin['unpaid_orders']} order(s) not yet paid",
                f"{fin['unpaid_value']:,.2f} {cur} is outstanding.",
                f"{fin['unpaid_orders']} unpaid",
                "Send payment reminders or cancel abandoned orders.",
                impact="Unpaid orders tie up stock that could be sold to someone else.",
                steps=[
                    "In Shopify Admin filter orders by payment status 'Pending'.",
                    "Send the invoice again; cancel anything older than 7 days to release the stock.",
                ],
            )
        )
    if fin["total_revenue"] and _pct(fin["refunds"], fin["gross_sales"]) > 5:
        actions.append(
            _action(
                "finance",
                "medium",
                f"Refunds are {_pct(fin['refunds'], fin['gross_sales']):.0f}% of gross sales",
                f"{fin['refunds']:,.2f} {cur} has been refunded.",
                "> 5% refund rate",
                "Look for a product or shipping problem behind the returns.",
                impact="Refunds cost the sale, the shipping and often the product itself.",
                steps=["Group refunded orders by product.", "Fix the description, sizing or packaging of the worst offender."],
            )
        )
    if fin["expenses_by_category"]:
        top_cat, top_amt = next(iter(fin["expenses_by_category"].items()))
        share = _pct(top_amt, fin["total_expenses"])
        if share >= 50 and fin["total_expenses"]:
            actions.append(
                _action(
                    "finance",
                    "low",
                    f"{top_cat} is {share:.0f}% of all expenses",
                    f"{top_amt:,.2f} {cur} - the single biggest lever on profit.",
                    f"{share:.0f}% of expenses",
                    "Negotiate or restructure this cost first.",
                    impact="A 10% saving here moves net profit more than any other category.",
                    steps=["Open Finance analysis → Expenses by category.", "Benchmark this cost against two alternative suppliers or rates."],
                )
            )

    rank = {sev: i for i, sev in enumerate(SEVERITIES)}
    actions.sort(key=lambda a: (rank[a["severity"]], DOMAINS.index(a["domain"])))
    return actions


async def priority_actions(db: AsyncSession) -> dict:
    summaries = await all_summaries(db)
    actions = derive_priority_actions(summaries)
    by_domain = {d: [a for a in actions if a["domain"] == d] for d in DOMAINS}
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "currency": summaries["finance"]["currency"],
        "counts": {sev: sum(1 for a in actions if a["severity"] == sev) for sev in SEVERITIES},
        "actions": actions,
        "by_domain": by_domain,
    }


# --------------------------------------------------------------------------- #
# business health score
# --------------------------------------------------------------------------- #


def _component(label: str, score: float, weight: float, detail: str) -> dict:
    return {"label": label, "score": round(_clamp(score)), "weight": weight, "detail": detail}


def _weighted(components: list[dict]) -> int:
    total_w = sum(c["weight"] for c in components) or 1
    return round(sum(c["score"] * c["weight"] for c in components) / total_w)


def _status(score: int) -> str:
    if score >= 80:
        return "healthy"
    if score >= 60:
        return "watch"
    return "at_risk"


def _grade(score: int) -> str:
    return "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"


def derive_health_score(s: dict[str, dict]) -> dict:
    inv, mkt, ops, fin = s["inventory"], s["marketing"], s["operations"], s["finance"]
    domains: dict[str, dict] = {}

    # Inventory: availability, stock-outs, data completeness
    skus = inv["total_skus"] or 1
    comps = [
        _component("In-stock rate", 100 * (1 - inv["low_stock_count"] / skus), 0.5, f"{skus - inv['low_stock_count']} of {inv['total_skus']} SKUs above reorder level"),
        _component("Stock-outs", 100 * (1 - min(inv["out_of_stock_count"] / skus * 4, 1)), 0.3, f"{inv['out_of_stock_count']} variant(s) at zero"),
        _component("Cost data", 100 * (1 - inv["missing_cost_count"] / skus), 0.2, f"{inv['missing_cost_count']} variant(s) without a unit cost"),
    ]
    domains["inventory"] = {"score": _weighted(comps), "components": comps}

    # Marketing: return on spend when there is spend; otherwise attribution and momentum
    if mkt["total_spend"] > 0 and mkt["roas"] is not None:
        comps = [
            _component("ROAS", mkt["roas"] / 3 * 100, 0.6, f"{mkt['roas']}x return on ad spend (3x = full marks)"),
            _component("Active campaigns", 100 if mkt["active_campaigns"] else 20, 0.2, f"{mkt['active_campaigns']} active"),
            _component("Click-through", (mkt["ctr_pct"] or 0) / 1.5 * 100, 0.2, f"{mkt['ctr_pct']}% CTR (1.5% = full marks)"),
        ]
    else:
        active_share = mkt["active_campaigns"] / mkt["total_campaigns"] * 100 if mkt["total_campaigns"] else 0
        comps = [
            _component("Attribution coverage", mkt["attribution_rate_pct"], 0.4, f"{mkt['attribution_rate_pct']}% of orders traceable to a campaign or code"),
            _component("Channel momentum", active_share, 0.4, f"{mkt['active_campaigns']} of {mkt['total_campaigns']} channels produced an order in 30 days"),
            _component("Converting channels", 100 if mkt["total_conversions"] else 0, 0.2, f"{mkt['total_conversions']} attributed order(s)"),
        ]
    domains["marketing"] = {"score": _weighted(comps), "components": comps}

    # Operations: fulfilment, backlog, task discipline
    open_tasks = ops["open_tasks"] or 0
    comps = [
        _component("Fulfilment rate", ops["fulfillment_rate_pct"], 0.5, f"{ops['fulfillment_rate_pct']}% of live orders fulfilled"),
        _component("Backlog", 100 - min(ops["pending_orders"] * 15, 100), 0.25, f"{ops['pending_orders']} order(s) awaiting fulfilment"),
        _component("Task discipline", 100 - min(ops["overdue_tasks"] * 25 + ops["high_priority_tasks"] * 10, 100), 0.15, f"{ops['overdue_tasks']} overdue, {ops['high_priority_tasks']} high priority of {open_tasks} open"),
        _component("Cancellations", 100 - min(ops["cancellation_rate_pct"] * 5, 100), 0.1, f"{ops['cancellation_rate_pct']}% of orders cancelled"),
    ]
    domains["operations"] = {"score": _weighted(comps), "components": comps}

    # Finance: margin, gross margin, collections
    comps = [
        _component("Net margin", max(fin["profit_margin_pct"], 0) / 20 * 100, 0.45, f"{fin['profit_margin_pct']}% net margin (20% = full marks)"),
        _component("Gross margin", max(fin["gross_margin_pct"], 0) / 40 * 100, 0.3, f"{fin['gross_margin_pct']}% after cost of goods (40% = full marks)"),
        _component("Collections", 100 if not fin["unpaid_orders"] else 100 - min(fin["unpaid_orders"] * 20, 100), 0.15, f"{fin['unpaid_orders']} unpaid order(s)"),
        _component("Refunds", 100 - min(_pct(fin["refunds"], fin["gross_sales"]) * 10, 100), 0.1, f"{_pct(fin['refunds'], fin['gross_sales'])}% of gross sales refunded"),
    ]
    domains["finance"] = {"score": _weighted(comps), "components": comps}

    for d in domains.values():
        d["status"] = _status(d["score"])
        d["grade"] = _grade(d["score"])
    overall = round(sum(d["score"] for d in domains.values()) / len(domains))
    return {
        "overall": {"score": overall, "status": _status(overall), "grade": _grade(overall)},
        "domains": domains,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


async def health_score(db: AsyncSession) -> dict:
    return derive_health_score(await all_summaries(db))
