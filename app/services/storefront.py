"""Customer-safe views of store data, used by the customer support agent.

Everything returned here is safe to repeat back to a shopper. Unlike
``insights``, it never exposes unit cost, margins, campaign figures or
whole-store financials, and it never lists orders in bulk — an order is only
reachable by its own order number.
"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Product

PRODUCT_SEARCH_LIMIT = 10

STATUS_MEANING = {
    "pending": "We have your order and it is queued for packing.",
    "processing": "Your order is being packed and will ship shortly.",
    "fulfilled": "Your order has shipped.",
    "cancelled": "This order was cancelled.",
}


def _availability(p: Product) -> str:
    if p.stock_qty <= 0:
        return "out_of_stock"
    if p.stock_qty <= p.reorder_level:
        return "low_stock"
    return "in_stock"


def _public_product(p: Product) -> dict:
    """A product as a shopper may see it — price and availability, never cost."""
    return {
        "sku": p.sku,
        "name": p.name,
        "category": p.category,
        "price": round(p.price, 2),
        "availability": _availability(p),
        "units_available": max(p.stock_qty, 0),
    }


def order_number_variants(raw: str) -> list[str]:
    """Accept '#1027', '1027' or 'ORD-1001' for the same order."""
    given = raw.strip()
    bare = given.lstrip("#").strip()
    return list(dict.fromkeys([given, bare, f"#{bare}", f"ORD-{bare}"]))


async def search_products(db: AsyncSession, query: str = "", limit: int = PRODUCT_SEARCH_LIMIT) -> dict:
    stmt = select(Product)
    term = query.strip()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(
            or_(Product.name.ilike(like), Product.category.ilike(like), Product.sku.ilike(like))
        )
    products = (await db.execute(stmt.order_by(Product.name).limit(limit))).scalars().all()
    return {
        "query": term,
        "count": len(products),
        "products": [_public_product(p) for p in products],
    }


async def list_categories(db: AsyncSession) -> dict:
    rows = (
        await db.execute(
            select(Product.category, func.count(Product.id)).group_by(Product.category).order_by(Product.category)
        )
    ).all()
    return {"categories": [{"name": name, "product_count": count} for name, count in rows]}


async def find_order(db: AsyncSession, order_number: str) -> dict:
    """Look one order up by its number. Returns a not-found marker rather than raising."""
    order = (
        await db.execute(select(Order).where(Order.order_number.in_(order_number_variants(order_number))))
    ).scalars().first()
    if order is None:
        return {"found": False, "order_number": order_number.strip()}
    return {
        "found": True,
        "order_number": order.order_number,
        "status": order.status,
        "status_meaning": STATUS_MEANING.get(order.status, "Please contact support for the current status."),
        "total": round(order.total, 2),
        "placed_on": order.created_at.date().isoformat() if order.created_at else None,
    }
