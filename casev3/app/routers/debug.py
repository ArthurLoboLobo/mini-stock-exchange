import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_key
from app.database import async_session, get_db
from app.engine import engine
from app.engine.persistence import run_persistence_loop

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
    """Full state reset: stop persistence, clear memory + DB, restart persistence."""
    # 1. Stop persistence task
    if engine.persistence_task is not None:
        engine.persistence_task.cancel()
        try:
            await engine.persistence_task
        except asyncio.CancelledError:
            pass
        engine.persistence_task = None

    # 2. Clear all in-memory state
    engine.clear()

    # 3. Truncate all DB tables
    await db.execute(text("DELETE FROM trades"))
    await db.execute(text("DELETE FROM orders"))
    await db.execute(text("DELETE FROM brokers"))
    await db.commit()

    # 4. Restart persistence task
    engine.persistence_task = asyncio.create_task(
        run_persistence_loop(engine, async_session)
    )

    return {"status": "database reset"}
