import hashlib
import hmac
import uuid

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.engine import engine

security = HTTPBearer()


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def get_current_broker_id(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> uuid.UUID:
    """Authenticate broker via in-memory lookup. Returns broker_id (UUID)."""
    key_hash = hash_api_key(credentials.credentials)
    broker_id = engine.brokers_by_key_hash.get(key_hash)
    if broker_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return broker_id


async def require_admin_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> None:
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin API key not configured")
    if not hmac.compare_digest(credentials.credentials, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Invalid admin API key")
