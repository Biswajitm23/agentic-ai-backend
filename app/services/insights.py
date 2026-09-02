"""Domain analytics shared by the dashboard endpoints and the agent tools."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Expense, OpsTask, Order, Product


async def inventory_summary(db: AsyncSession) -> dict:
    products = (await db.execute(select(Product))).scalars().all()
    low_stock = [p for p in products if p.stock_qty <= p.reorder_level]
    return {
        "total_skus": len(products),
        "total_units": sum(p.stock_qty for p in products),
        "stock_value": round(sum(p.stock_qty * p.cost for p in products), 2),
        "low_stock_count": len(low_stock),
        "low_stock_items": [
            {"sku": p.sku, "name": p.name, "stock_qty": p.stock_qty, "reorder_level": p.reorder_level}
            for p in low_stock
        ],
        "products": [
            {
                "sku": p.sku,
                "name": p.name,
                "category": p.category,
                "price": p.price,
                "cost": p.cost,
                "stock_qty": p.stock_qty,
                "reorder_level": p.reorder_level,
                "low_stock": p.stock_qty <= p.reorder_level,
            }
            for p in products
        ],
    }


async def marketing_summary(db: AsyncSession) -> dict:
    campaigns = (await db.execute(select(Campaign))).scalars().all()
    spend = sum(c.spend for c in campaigns)
    revenue = sum(c.revenue for c in campaigns)
    clicks = sum(c.clicks for c in campaigns)
    impressions = sum(c.impressions for c in campaigns)
    return {
        "total_campaigns": len(campaigns),
        "active_campaigns": sum(1 for c in campaigns if c.status == "active"),
        "total_spend": round(spend, 2),
        "total_revenue": round(revenue, 2),
        "roas": round(revenue / spend, 2) if spend else 0,
        "ctr_pct": round(clicks / impressions * 100, 2) if impressions else 0,
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
                "roas": round(c.revenue / c.spend, 2) if c.spend else 0,
            }
            for c in campaigns
        ],
    }


async def operations_summary(db: AsyncSession) -> dict:
    orders = (await db.execute(select(Order))).scalars().all()
    tasks = (await db.execute(select(OpsTask))).scalars().all()
    by_status: dict[str, int] = {}
    for o in orders:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    return {
        "total_orders": len(orders),
        "orders_by_status": by_status,
        "pending_orders": by_status.get("pending", 0) + by_status.get("processing", 0),
        "open_tasks": sum(1 for t in tasks if t.status != "done"),
        "high_priority_tasks": sum(1 for t in tasks if t.priority == "high" and t.status != "done"),
        "tasks": [
            {
                "title": t.title,
                "priority": t.priority,
                "status": t.status,
                "due_date": t.due_date.isoformat() if t.due_date else None,
            }
            for t in tasks
        ],
        "recent_orders": [
            {"order_number": o.order_number, "customer_name": o.customer_name, "total": o.total, "status": o.status}
            for o in sorted(orders, key=lambda o: o.id, reverse=True)[:10]
        ],
    }


async def finance_summary(db: AsyncSession) -> dict:
    order_revenue = (
        await db.execute(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status != "cancelled"))
    ).scalar_one()
    expenses = (await db.execute(select(Expense))).scalars().all()
    campaigns = (await db.execute(select(Campaign))).scalars().all()
    ad_revenue = sum(c.revenue for c in campaigns)
    total_revenue = round(order_revenue + ad_revenue, 2)
    total_expenses = round(sum(e.amount for e in expenses), 2)
    net_profit = round(total_revenue - total_expenses, 2)
    by_category: dict[str, float] = {}
    for e in expenses:
        by_category[e.category] = round(by_category.get(e.category, 0) + e.amount, 2)
    return {
        "total_revenue": total_revenue,
        "order_revenue": round(order_revenue, 2),
        "campaign_revenue": round(ad_revenue, 2),
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "profit_margin_pct": round(net_profit / total_revenue * 100, 2) if total_revenue else 0,
        "expenses_by_category": by_category,
        "expenses": [
            {
                "category": e.category,
                "description": e.description,
                "amount": e.amount,
                "date": e.expense_date.isoformat(),
            }
            for e in expenses
        ],
    }


SUMMARY_FUNCS = {
    "inventory": inventory_summary,
    "marketing": marketing_summary,
    "operations": operations_summary,
    "finance": finance_summary,
}
