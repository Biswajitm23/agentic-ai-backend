from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import insights
from app.services.insights import SUMMARY_FUNCS

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
async def dashboard_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Headline KPIs for all four domains, shown in the top section of the dashboard."""
    inventory = await insights.inventory_summary(db)
    marketing = await insights.marketing_summary(db)
    operations = await insights.operations_summary(db)
    finance = await insights.finance_summary(db)
    return {
        "inventory": {k: inventory[k] for k in ("total_skus", "total_units", "stock_value", "low_stock_count")},
        "marketing": {k: marketing[k] for k in ("active_campaigns", "total_spend", "total_revenue", "roas")},
        "operations": {k: operations[k] for k in ("total_orders", "pending_orders", "open_tasks", "high_priority_tasks")},
        "finance": {k: finance[k] for k in ("total_revenue", "total_expenses", "net_profit", "profit_margin_pct")},
    }


@router.get("/analysis/{domain}")
async def domain_analysis(domain: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Full detail for one domain, loaded when its accordion is opened."""
    func = SUMMARY_FUNCS.get(domain)
    if func is None:
        raise HTTPException(status_code=404, detail=f"Unknown domain '{domain}'")
    return await func(db)
