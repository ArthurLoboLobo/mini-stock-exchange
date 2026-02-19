import asyncio
import logging

import httpx

from app.schemas import WebhookPayload

logger = logging.getLogger(__name__)


async def send_webhook(url: str, payload: WebhookPayload) -> None:
    """Fire-and-forget webhook delivery. Logs failures, no retries."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json=payload.model_dump(mode="json"))
            if response.status_code >= 400:
                logger.warning(
                    "Webhook delivery failed: url=%s status=%d",
                    url, response.status_code,
                )
    except Exception:
        logger.exception("Webhook delivery error: url=%s", url)


def fire_webhooks(webhooks: list[tuple[str, WebhookPayload]]) -> None:
    """Schedule webhook deliveries as background tasks."""
    for url, payload in webhooks:
        asyncio.create_task(send_webhook(url, payload))
