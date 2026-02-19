import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_broker_id, hash_api_key, require_admin_key
from app.database import get_db
from app.engine import engine
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
    key_hash = hash_api_key(raw_key)
    broker = Broker(
        name=body.name,
        api_key_hash=key_hash,
        webhook_url=body.webhook_url,
    )
    db.add(broker)
    await db.commit()
    await db.refresh(broker)

    # Update in-memory broker auth
    engine.brokers_by_key_hash[key_hash] = broker.id

    return BrokerRegistered(broker_id=broker.id, api_key=raw_key)


@router.get("/balance", response_model=BrokerBalance)
async def get_balance(
    broker_id: uuid.UUID = Depends(get_current_broker_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Broker).where(Broker.id == broker_id)
    )
    broker = result.scalar_one()

    return BrokerBalance(
        broker_id=broker.id,
        broker_name=broker.name,
        balance=broker.balance,
    )
