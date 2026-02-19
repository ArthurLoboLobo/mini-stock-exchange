import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_broker_id, hash_api_key, require_admin_key
from app.database import get_db
from app.engine import BrokerInfo, engine
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

    # Update in-memory broker auth + info
    engine.brokers_by_key_hash[key_hash] = broker.id
    engine.brokers[broker.id] = BrokerInfo(
        name=broker.name, balance=0, webhook_url=body.webhook_url,
    )

    return BrokerRegistered(broker_id=broker.id, api_key=raw_key)


@router.get("/balance", response_model=BrokerBalance)
async def get_balance(
    broker_id: uuid.UUID = Depends(get_current_broker_id),
):
    broker_info = engine.brokers.get(broker_id)
    if broker_info is None:
        raise HTTPException(status_code=404, detail="Broker not found")

    return BrokerBalance(
        broker_id=broker_id,
        broker_name=broker_info.name,
        balance=broker_info.balance,
    )
