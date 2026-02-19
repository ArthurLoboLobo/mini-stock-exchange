from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_key
from app.database import get_db

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/trade-count")
async def trade_count(
    _=Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
):
    """Return the current number of trades in the database."""
    result = await db.execute(text("SELECT COUNT(*) FROM trades"))
    count = result.scalar()
    return {"count": count}


@router.post("/reset")
async def reset_database(
    _=Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
):
    """Delete all trades, orders, and brokers. Full database reset for benchmarks."""
    await db.execute(text("DELETE FROM trades"))
    await db.execute(text("DELETE FROM orders"))
    await db.execute(text("DELETE FROM brokers"))
    await db.commit()
    return {"status": "database reset"}
