"""Pulls live products and orders from the store's private custom app into the database.

The dashboard, analysis endpoints and the admin agent read from the database, so
a sync makes those layers reflect the real store. The customer support agent does
not go through here - it reads Shopify live, via ``shopify_storefront``.
"""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Product
from app.services.shopify_client import graphql, is_configured  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

PRODUCTS_QUERY = """
{
  products(first: 100) {
    edges {
      node {
        title
        productType
        variants(first: 50) {
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
{
  orders(first: 100, sortKey: CREATED_AT, reverse: true) {
    edges {
      node {
        name
        cancelledAt
        displayFulfillmentStatus
        totalPriceSet { shopMoney { amount } }
      }
    }
  }
}
"""


def _order_status(node: dict) -> str:
    if node.get("cancelledAt"):
        return "cancelled"
    fulfillment = (node.get("displayFulfillmentStatus") or "").upper()
    if fulfillment == "FULFILLED":
        return "fulfilled"
    if fulfillment in ("IN_PROGRESS", "PARTIALLY_FULFILLED"):
        return "processing"
    return "pending"


async def sync_shopify(db: AsyncSession) -> dict:
    """Upsert Shopify products (per variant) and orders into SQLite. Returns counts."""
    if not is_configured():
        raise RuntimeError("Shopify is not configured: set SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN")

    async with httpx.AsyncClient() as client:
        products_data = await graphql(PRODUCTS_QUERY, client=client)
        orders_data = await graphql(ORDERS_QUERY, client=client)

    existing_products = {
        p.sku: p for p in (await db.execute(select(Product))).scalars().all()
    }
    product_count = 0
    for p_edge in products_data["products"]["edges"]:
        p = p_edge["node"]
        for v_edge in p["variants"]["edges"]:
            v = v_edge["node"]
            sku = v["sku"] or v["id"].rsplit("/", 1)[-1]
            name = p["title"] if v["title"] in (None, "Default Title") else f"{p['title']} - {v['title']}"
            unit_cost = (v.get("inventoryItem") or {}).get("unitCost") or {}
            row = existing_products.get(sku)
            if row is None:
                row = Product(sku=sku, reorder_level=10)
                db.add(row)
                existing_products[sku] = row
            row.name = name
            row.category = p["productType"] or "Uncategorized"
            row.price = float(v["price"] or 0)
            row.cost = float(unit_cost.get("amount") or 0)
            row.stock_qty = int(v["inventoryQuantity"] or 0)
            product_count += 1

    existing_orders = {
        o.order_number: o for o in (await db.execute(select(Order))).scalars().all()
    }
    order_count = 0
    for o_edge in orders_data["orders"]["edges"]:
        o = o_edge["node"]
        number = o["name"]
        row = existing_orders.get(number)
        if row is None:
            row = Order(order_number=number)
            db.add(row)
            existing_orders[number] = row
        # customer details need the read_customers scope, which this app doesn't request
        row.customer_name = row.customer_name or "—"
        row.total = float(o["totalPriceSet"]["shopMoney"]["amount"])
        row.status = _order_status(o)
        order_count += 1

    await db.commit()
    return {"products_synced": product_count, "orders_synced": order_count}
