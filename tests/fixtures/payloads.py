"""Meta-shaped WhatsApp webhook payloads.

The JSON under ``whatsapp/`` follows the shapes in Meta's webhook reference, with fake ids
and the PII corpus's phone number, so a leak test can look for it. Replace each with a
redacted real capture once the live hookup runs (docs/runbook-iteration-02.md).
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Final

from personal_organizer.providers.channel.hmac_sha256 import sign

DIR: Final = Path(__file__).resolve().parent / "whatsapp"

#: The phone_number_id every fixture is addressed to.
PHONE_NUMBER_ID: Final = "106540352242922"
#: The sender every fixture comes from, as Meta spells it (no ``+``).
WA_ID: Final = "31612345678"
SENDER_PHONE: Final = "+31612345678"
APP_SECRET: Final = b"test-app-secret"


def load(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((DIR / f"{name}.json").read_text())
    return payload


def value_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The ``value`` object of the first change -- where messages and statuses live."""
    value: dict[str, Any] = payload["entry"][0]["changes"][0]["value"]
    return value


def text_message(
    *,
    wamid: str = "wamid.TEST1",
    from_: str = WA_ID,
    body: str = "hello",
    timestamp: int | None = None,
) -> dict[str, Any]:
    """The text fixture with the fields a test usually needs to vary."""
    payload = copy.deepcopy(load("text"))
    value = value_of(payload)
    value["contacts"][0]["wa_id"] = from_
    message = value["messages"][0]
    message.update(
        id=wamid,
        text={"body": body},
        timestamp=str(int(time.time()) if timestamp is None else timestamp),
    )
    message["from"] = from_
    return payload


def encode(payload: dict[str, Any]) -> bytes:
    """Serialise the way Meta does -- not canonical, which is why signatures use raw bytes."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def signed(payload: dict[str, Any], secret: bytes = APP_SECRET) -> tuple[bytes, dict[str, str]]:
    body = encode(payload)
    return body, {
        "content-type": "application/json",
        "x-hub-signature-256": sign(secret, body),
    }
