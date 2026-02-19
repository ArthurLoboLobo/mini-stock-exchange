import asyncio
import logging

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger(__name__)

_cleanup_task: asyncio.Task | None = None
CLEANUP_INTERVAL_SECONDS = 60


async def _expire_orders_loop():
    """Periodically close expired orders to keep the partial index compact."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            async with async_session() as session:
                result = await session.execute(
                    text("""
                        UPDATE orders SET status = 'closed'
                        WHERE status = 'open' AND valid_until < NOW()
                    """)
                )
                await session.commit()
                if result.rowcount > 0:
                    logger.info("Expired %d orders", result.rowcount)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in expiration cleanup")


def start_expiration_cleanup():
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_expire_orders_loop())


async def stop_expiration_cleanup():
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
