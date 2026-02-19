import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_broker, hash_api_key, require_admin_key
from app.database import get_db
from app.models import Broker
from app.schemas import BrokerBalance, BrokerRegister, BrokerRegistered

router = APIRouter()


@router.post(
    "/register",
    response_model=BrokerRegistered,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_key)],
)
async def register_broker(
    body: BrokerRegister,
    db: AsyncSession = Depends(get_db),
):
    raw_key = f"key-{uuid.uuid4()}"
    broker = Broker(
        name=body.name,
        api_key_hash=hash_api_key(raw_key),
        webhook_url=body.webhook_url,
    )
    db.add(broker)
    await db.commit()
    await db.refresh(broker)
    return BrokerRegistered(broker_id=broker.id, api_key=raw_key)


@router.get("/balance", response_model=BrokerBalance)
async def get_balance(
    broker: Broker = Depends(get_current_broker),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""
            SELECT
                COALESCE((SELECT SUM(t.price::BIGINT * t.quantity)
                          FROM trades t JOIN orders o ON o.id = t.sell_order_id
                          WHERE o.broker_id = :broker_id), 0)
                -
                COALESCE((SELECT SUM(t.price::BIGINT * t.quantity)
                          FROM trades t JOIN orders o ON o.id = t.buy_order_id
                          WHERE o.broker_id = :broker_id), 0) as balance
        """),
        {"broker_id": str(broker.id)},
    )
    row = result.one()

    return BrokerBalance(
        broker_id=broker.id,
        broker_name=broker.name,
        balance=row.balance,
    )
