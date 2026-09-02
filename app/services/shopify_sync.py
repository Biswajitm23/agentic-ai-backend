"""Pulls live products and orders from the store's private custom app into SQLite.

The dashboard, analysis endpoints and agent tools all read from SQLite, so a sync
makes the whole system reflect the real store without touching those layers.
"""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Order, Product

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


def is_configured() -> bool:
    return bool(settings.SHOPIFY_STORE_URL and settings.SHOPIFY_ACCESS_TOKEN)


def _graphql_url() -> str:
    host = settings.SHOPIFY_STORE_URL.removeprefix("https://").removeprefix("http://").strip("/")
    return f"https://{host}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"


async def _graphql(client: httpx.AsyncClient, query: str) -> dict:
    resp = await client.post(
        _graphql_url(),
        json={"query": query},
        headers={"X-Shopify-Access-Token": settings.SHOPIFY_ACCESS_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")
    return payload["data"]


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
        products_data = await _graphql(client, PRODUCTS_QUERY)
        orders_data = await _graphql(client, ORDERS_QUERY)

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
