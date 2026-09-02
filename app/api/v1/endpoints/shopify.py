import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services import shopify_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shopify"])


@router.get("/shopify/status")
async def shopify_status() -> dict:
    return {
        "configured": shopify_sync.is_configured(),
        "store_url": settings.SHOPIFY_STORE_URL or None,
    }


@router.post("/shopify/sync")
async def trigger_sync(db: AsyncSession = Depends(get_db)) -> dict:
    if not shopify_sync.is_configured():
        raise HTTPException(status_code=400, detail="Shopify is not configured (missing store URL or access token)")
    try:
        result = await shopify_sync.sync_shopify(db)
    except Exception as exc:
        logger.exception("Shopify sync failed")
        raise HTTPException(status_code=502, detail=f"Shopify sync failed: {exc}") from exc
    return {"status": "ok", **result}
