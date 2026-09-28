"""Meta webhook payload -> :class:`WebhookBatch`.

One pure function, total by design: whatever arrives, it returns a batch and never raises.
Its input has already passed signature verification, so a shape it does not expect is Meta
changing something, not an attacker -- and the right response to that is to store what can
be stored, count what cannot, and answer 200. An exception here would answer 500 and Meta
would redeliver the same payload for seven days.

The envelope::

    {"object": "whatsapp_business_account",
     "entry": [{"id": "<WABA id>",
                "changes": [{"field": "messages",
                             "value": {"messaging_product": "whatsapp",
                                       "metadata": {"display_phone_number": "...",
                                                    "phone_number_id": "..."},
                                       "contacts": [{"wa_id": "...", "profile": {...}}],
                                       "messages": [...],
                                       "statuses": [...]}}]}]}

**The one known unknown is the business-scoped user id (BSUID).** Meta's username feature
lets a user hide their phone number, in which case payloads identify them by a BSUID
instead. :data:`BSUID_KEYS` lists the field names it may arrive under; confirm it against a
real payload (docs/runbook-iteration-02.md, last step) -- it is the only line to change.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

from personal_organizer.core.phone import normalise_e164
from personal_organizer.interfaces.channel import (
    DeliveryStatus,
    DeliveryUpdate,
    InboundMessage,
    SenderRef,
    WebhookBatch,
)

WEBHOOK_OBJECT: Final = "whatsapp_business_account"
MESSAGES_FIELD: Final = "messages"

#: Candidate field names for the business-scoped user id, checked on the message first and
#: then on its matching contact. Unconfirmed until a real payload is seen.
BSUID_KEYS: Final = ("user_id", "bsuid", "from_user_id")

#: Types whose payload object carries ``id`` and ``mime_type`` (and sometimes ``caption``).
_MEDIA_TYPES: Final = frozenset({"audio", "image", "video", "document", "sticker"})

_STATUSES: Final = {status.value: status for status in DeliveryStatus}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                yield item


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> datetime:
    """Meta sends Unix seconds as a string. Anything unreadable becomes "now"."""
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC)
    except TypeError, ValueError, OverflowError, OSError:
        return datetime.now(UTC)


def _user_id(*sources: Mapping[str, Any]) -> str | None:
    for source in sources:
        for key in BSUID_KEYS:
            if value := _text(source.get(key)):
                return value
    return None


def _sender(message: Mapping[str, Any], contacts: Mapping[str, Mapping[str, Any]]) -> SenderRef:
    raw_from = _text(message.get("from"))
    contact = contacts.get(raw_from or "", {})
    user_id = _user_id(message, contact)
    phone = normalise_e164(raw_from) or normalise_e164(contact.get("wa_id"))
    if phone is None and user_id is None and raw_from is not None:
        # A non-numeric ``from`` with no BSUID field we recognise: it is the BSUID under a
        # name we have not seen. Keep it as the identity rather than dropping the message.
        user_id = raw_from
    return SenderRef(user_id=user_id, phone=phone)


class _Content(TypedDict, total=False):
    text: str | None
    media_id: str | None
    media_mime_type: str | None
    reply_id: str | None


def _content(message: Mapping[str, Any], message_type: str) -> _Content:
    """The type-specific fields: text, media and interactive reply ids."""
    body = _mapping(message.get(message_type))
    if message_type == "text":
        return {"text": _text(body.get("body"))}
    if message_type in _MEDIA_TYPES:
        return {
            "media_id": _text(body.get("id")),
            "media_mime_type": _text(body.get("mime_type")),
            "text": _text(body.get("caption")),
        }
    if message_type == "interactive":
        # Exactly one of button_reply / list_reply, named by ``interactive.type``.
        reply = _mapping(body.get(str(body.get("type"))))
        return {"reply_id": _text(reply.get("id")), "text": _text(reply.get("title"))}
    if message_type == "button":
        # A quick-reply button on a template message: the payload is what we set.
        return {"reply_id": _text(body.get("payload")), "text": _text(body.get("text"))}
    return {}


def _message(
    message: Mapping[str, Any], contacts: Mapping[str, Mapping[str, Any]]
) -> InboundMessage | None:
    provider_message_id = _text(message.get("id"))
    if provider_message_id is None:
        return None
    sender = _sender(message, contacts)
    if sender.user_id is None and sender.phone is None:
        return None
    message_type = _text(message.get("type")) or "unknown"
    return InboundMessage(
        provider_message_id=provider_message_id,
        sender=sender,
        sent_at=_timestamp(message.get("timestamp")),
        message_type=message_type,
        context_message_id=_text(_mapping(message.get("context")).get("id")),
        raw=dict(message),
        **_content(message, message_type),
    )


def _update(status: Mapping[str, Any]) -> DeliveryUpdate | None:
    provider_message_id = _text(status.get("id"))
    state = _STATUSES.get(str(status.get("status")))
    if provider_message_id is None or state is None:
        return None
    codes = (error.get("code") for error in _mappings(status.get("errors")))
    error_code = next((code for code in codes if isinstance(code, int)), None)
    return DeliveryUpdate(
        provider_message_id=provider_message_id,
        status=state,
        at=_timestamp(status.get("timestamp")),
        error_code=error_code,
    )


def parse_webhook(payload: object, *, phone_number_id: str | None) -> WebhookBatch:
    """Extract the messages and delivery updates addressed to ``phone_number_id``.

    Changes for any other phone number id are skipped rather than processed: a WABA can hold
    several numbers, and an app subscribed to it hears about all of them.
    """
    envelope = _mapping(payload)
    if envelope.get("object") != WEBHOOK_OBJECT:
        return WebhookBatch(skipped=1)

    messages: list[InboundMessage] = []
    updates: list[DeliveryUpdate] = []
    skipped = 0
    for entry in _mappings(envelope.get("entry")):
        for change in _mappings(entry.get("changes")):
            value = _mapping(change.get("value"))
            addressed_to = _mapping(value.get("metadata")).get("phone_number_id")
            if change.get("field") != MESSAGES_FIELD or addressed_to != phone_number_id:
                skipped += 1
                continue
            contacts = {
                str(contact.get("wa_id")): contact for contact in _mappings(value.get("contacts"))
            }
            for raw_message in _mappings(value.get("messages")):
                if (message := _message(raw_message, contacts)) is None:
                    skipped += 1
                else:
                    messages.append(message)
            for raw_status in _mappings(value.get("statuses")):
                if (update := _update(raw_status)) is None:
                    skipped += 1
                else:
                    updates.append(update)
    return WebhookBatch(messages=tuple(messages), updates=tuple(updates), skipped=skipped)


__all__ = ["BSUID_KEYS", "MESSAGES_FIELD", "WEBHOOK_OBJECT", "parse_webhook"]
