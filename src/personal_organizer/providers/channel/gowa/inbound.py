"""The api-side half of the GOWA channel: verify and parse. No network access."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from personal_organizer.interfaces.channel import WebhookBatch
from personal_organizer.providers.channel.gowa.parser import parse_webhook
from personal_organizer.providers.channel.hmac_sha256 import verify_signature
from personal_organizer.settings import GowaSettings

CHANNEL_NAME: Final = "gowa"


@dataclass(frozen=True)
class GowaInbound:
    webhook_secret: bytes | None = field(repr=False)
    device_id: str | None
    name: str = CHANNEL_NAME

    @classmethod
    def from_settings(cls, settings: GowaSettings) -> GowaInbound:
        secret = settings.webhook_secret
        return cls(
            webhook_secret=secret.get_secret_value().encode() if secret else None,
            device_id=settings.device_id,
        )

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        return verify_signature(self.webhook_secret, raw_body, signature)

    def parse_webhook(self, payload: object) -> WebhookBatch:
        return parse_webhook(payload, device_id=self.device_id)


__all__ = ["CHANNEL_NAME", "GowaInbound"]
