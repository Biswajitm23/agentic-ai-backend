from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Expense, OpsTask, Order, Product

TODAY = date.today()


def _seed_store_data(session: AsyncSession) -> None:
    """Demo products and orders — only used when Shopify is not connected."""
    session.add_all(
        [
            Product(name="Classic Leather Wallet", sku="WAL-001", category="Accessories", price=49.0, cost=18.0, stock_qty=120, reorder_level=30),
            Product(name="Canvas Tote Bag", sku="TOT-002", category="Bags", price=29.0, cost=9.5, stock_qty=8, reorder_level=25),
            Product(name="Stainless Water Bottle", sku="BOT-003", category="Drinkware", price=24.0, cost=7.0, stock_qty=310, reorder_level=50),
            Product(name="Wireless Earbuds Pro", sku="EAR-004", category="Electronics", price=129.0, cost=54.0, stock_qty=15, reorder_level=20),
            Product(name="Organic Cotton T-Shirt", sku="TSH-005", category="Apparel", price=35.0, cost=11.0, stock_qty=240, reorder_level=60),
            Product(name="Ceramic Coffee Mug", sku="MUG-006", category="Drinkware", price=18.0, cost=5.0, stock_qty=4, reorder_level=40),
            Product(name="Fitness Resistance Bands", sku="FIT-007", category="Sports", price=22.0, cost=6.5, stock_qty=95, reorder_level=25),
            Product(name="Bamboo Desk Organizer", sku="DSK-008", category="Home Office", price=42.0, cost=16.0, stock_qty=58, reorder_level=15),
        ]
    )

    statuses = ["fulfilled"] * 6 + ["processing"] * 3 + ["pending"] * 4 + ["cancelled"]
    session.add_all(
        [
            Order(order_number=f"ORD-10{i:02d}", customer_name=name, total=total, status=status)
            for i, (name, total, status) in enumerate(
                zip(
                    ["Ava Patel", "Liam Chen", "Noah Garcia", "Emma Wilson", "Olivia Brown", "Ethan Davis", "Mia Martinez", "Lucas Kim", "Sophia Lee", "Jackson Wright", "Amelia Clark", "Harper Lewis", "Elijah Walker", "Isabella Hall"],
                    [129.0, 78.5, 245.0, 49.0, 322.0, 64.0, 158.0, 89.0, 41.0, 210.0, 96.0, 132.0, 57.0, 74.0],
                    statuses,
                )
            )
        ]
    )


async def seed_if_empty(session: AsyncSession, include_store_data: bool = True) -> None:
    """Seed demo data. When Shopify is connected, products and orders come from the
    store instead, so include_store_data=False seeds only the domains Shopify lacks
    (campaigns, tasks, expenses)."""
    count = (await session.execute(select(func.count(Campaign.id)))).scalar_one()
    if count:
        return

    if include_store_data:
        _seed_store_data(session)

    session.add_all(
        [
            Campaign(name="Summer Sale Blast", platform="Meta", status="active", budget=5000, spend=3620, impressions=412000, clicks=9800, conversions=430, revenue=15480),
            Campaign(name="Google Shopping Core", platform="Google", status="active", budget=8000, spend=6150, impressions=530000, clicks=12400, conversions=610, revenue=24900),
            Campaign(name="TikTok Creator Push", platform="TikTok", status="active", budget=3000, spend=2210, impressions=890000, clicks=15600, conversions=280, revenue=7840),
            Campaign(name="Retargeting Q3", platform="Meta", status="paused", budget=2000, spend=1980, impressions=150000, clicks=4300, conversions=190, revenue=6650),
            Campaign(name="Email Win-back", platform="Email", status="active", budget=800, spend=420, impressions=54000, clicks=6100, conversions=350, revenue=9100),
        ]
    )

    session.add_all(
        [
            OpsTask(title="Restock low inventory from supplier", priority="high", status="open", due_date=TODAY + timedelta(days=2)),
            OpsTask(title="Ship pending orders batch", priority="high", status="in_progress", due_date=TODAY + timedelta(days=1)),
            OpsTask(title="Quarterly supplier contract review", priority="medium", status="open", due_date=TODAY + timedelta(days=14)),
            OpsTask(title="Update SOP for returns handling", priority="low", status="open", due_date=TODAY + timedelta(days=21)),
            OpsTask(title="Warehouse cycle count - Zone B", priority="medium", status="done", due_date=TODAY - timedelta(days=3)),
        ]
    )

    session.add_all(
        [
            Expense(category="Advertising", description="Meta + Google ad spend", amount=9770, expense_date=TODAY - timedelta(days=5)),
            Expense(category="Logistics", description="3PL fulfillment fees", amount=2140, expense_date=TODAY - timedelta(days=7)),
            Expense(category="Software", description="SaaS subscriptions (Shopify, tools)", amount=640, expense_date=TODAY - timedelta(days=10)),
            Expense(category="Payroll", description="Part-time warehouse staff", amount=3800, expense_date=TODAY - timedelta(days=12)),
            Expense(category="Logistics", description="Inbound freight for restock", amount=1275, expense_date=TODAY - timedelta(days=15)),
        ]
    )

    await session.commit()
