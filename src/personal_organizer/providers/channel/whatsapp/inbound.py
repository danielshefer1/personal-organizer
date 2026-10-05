"""The api-side half of the WhatsApp channel: verify and parse. No network access."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from personal_organizer.interfaces.channel import WebhookBatch
from personal_organizer.providers.channel.hmac_sha256 import verify_signature
from personal_organizer.providers.channel.whatsapp.parser import parse_webhook
from personal_organizer.settings import WhatsAppSettings

CHANNEL_NAME: Final = "whatsapp"


@dataclass(frozen=True)
class WhatsAppInbound:
    app_secret: bytes | None = field(repr=False)
    phone_number_id: str | None
    name: str = CHANNEL_NAME

    @classmethod
    def from_settings(cls, settings: WhatsAppSettings) -> WhatsAppInbound:
        secret = settings.app_secret.get_secret_value().encode() if settings.app_secret else None
        return cls(app_secret=secret, phone_number_id=settings.phone_number_id)

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        return verify_signature(self.app_secret, raw_body, signature)

    def parse_webhook(self, payload: object) -> WebhookBatch:
        return parse_webhook(payload, phone_number_id=self.phone_number_id)


__all__ = ["CHANNEL_NAME", "WhatsAppInbound"]
