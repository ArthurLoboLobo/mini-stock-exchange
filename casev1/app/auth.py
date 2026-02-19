import hashlib
import hmac
import time

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Broker

security = HTTPBearer()

# TTL cache for broker auth lookups: api_key_hash -> (Broker, timestamp)
_broker_cache: dict[str, tuple[Broker, float]] = {}
_BROKER_CACHE_TTL = 60.0  # seconds


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def get_current_broker(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db: AsyncSession = Depends(get_db),
) -> Broker:
    key_hash = hash_api_key(credentials.credentials)

    # Check cache first
    now = time.monotonic()
    cached = _broker_cache.get(key_hash)
    if cached is not None:
        broker, cached_at = cached
        if now - cached_at < _BROKER_CACHE_TTL:
            return broker
        else:
            del _broker_cache[key_hash]

    result = await db.execute(select(Broker).where(Broker.api_key_hash == key_hash))
    broker = result.scalar_one_or_none()
    if broker is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Detach from session so cached object doesn't cause issues
    db.expunge(broker)
    _broker_cache[key_hash] = (broker, now)
    return broker


async def require_admin_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> None:
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin API key not configured")
    if not hmac.compare_digest(credentials.credentials, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Invalid admin API key")
