"""``po-gowa`` -- operator tools for the GOWA gateway channel.

``simulate``
    Sign and POST a GOWA-shaped text message to a webhook URL, as the gateway would.
    Exercises signature, ingress, queue, worker and reply attempt with no phone linked.
    Signs with ``GOWA__WEBHOOK_SECRET`` from the environment, which must match the target's.

``status``
    Ask the gateway whether its WhatsApp session is connected and logged in -- the first
    thing to check when replies stop (docs/runbook-whatsapp-gateway.md). The worker's
    ``system:gowa_health`` task asks the same question every five minutes (``status.py``).

``hash``
    Print the ``sender_hash`` a phone number appears under in the logs, given the target's
    ``LOGGING__PII_PEPPER``. Identical to ``po-whatsapp hash``: a person's key is the same
    on every channel.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from personal_organizer.core.phone import normalise_e164
from personal_organizer.providers.channel.gowa.parser import MESSAGE_EVENT, phone_jid
from personal_organizer.providers.channel.gowa.status import Unhealthy, check_status_sync
from personal_organizer.providers.channel.hmac_sha256 import SIGNATURE_HEADER, sign
from personal_organizer.providers.channel.whatsapp.cli import sender_hash
from personal_organizer.settings import Settings, get_settings


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def text_payload(*, from_: str, text: str, message_id: str, sent_at: datetime) -> dict[str, Any]:
    """A text message webhook in the shape the gateway documents."""
    jid = phone_jid(from_)
    return {
        "event": MESSAGE_EVENT,
        "device_id": "0@s.whatsapp.net",
        "payload": {
            "id": message_id,
            "chat_id": jid,
            "from": jid,
            "from_name": "Simulated",
            "timestamp": sent_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "is_from_me": False,
            "body": text,
        },
    }


def simulate(
    settings: Settings,
    *,
    url: str,
    from_: str,
    text: str,
    replay: int,
    client: httpx.Client,
) -> int:
    secret = settings.gowa.webhook_secret
    if secret is None:
        _out("GOWA__WEBHOOK_SECRET must be set to sign a message")
        return 2
    phone = normalise_e164(from_)
    if phone is None:
        _out("--from must be an international number, e.g. +31612345678")
        return 2
    payload = text_payload(
        from_=phone,
        text=text,
        message_id=f"SIMULATED{uuid.uuid4().hex.upper()}",
        sent_at=datetime.now(UTC),
    )
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {
        "content-type": "application/json",
        SIGNATURE_HEADER: sign(secret.get_secret_value().encode(), body),
    }
    failed = 0
    for attempt in range(1, replay + 1):
        response = client.post(url, content=body, headers=headers)
        _out(f"POST {attempt}/{replay} -> {response.status_code}")
        failed += response.status_code != httpx.codes.OK
    return 1 if failed else 0


def status(settings: Settings, *, client: httpx.Client) -> int:
    gowa = settings.gowa
    if gowa.basic_auth_user is None or gowa.basic_auth_password is None:
        _out("GOWA__BASIC_AUTH_USER and GOWA__BASIC_AUTH_PASSWORD must be set")
        return 2
    health = check_status_sync(client, gowa)
    if health.reason is Unhealthy.UNREACHABLE:
        _out(f"unreachable: {health.error_type} at {gowa.base_url}")
        return 1
    if health.reason is Unhealthy.BAD_RESPONSE:
        if health.status_code is not None:
            _out(f"failed: HTTP {health.status_code}")
        else:
            _out("failed: unexpected response from the gateway")
        return 1
    _out(f"connected={health.connected} logged_in={health.logged_in}")
    if health.reason is Unhealthy.NOT_LOGGED_IN:
        _out("not logged in: open the gateway's UI and scan the QR code from the bot's phone")
    return 0 if health.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="po-gowa")
    commands = parser.add_subparsers(dest="command", required=True)

    sim = commands.add_parser("simulate", help="sign and POST a text message webhook")
    sim.add_argument("--url", required=True, help="e.g. http://localhost:8000/webhooks/gowa")
    sim.add_argument("--from", dest="from_", required=True, help="sender, E.164")
    sim.add_argument("--text", default="hello from po-gowa simulate")
    sim.add_argument("--replay", type=int, default=1, help="POST the identical body N times")

    hashed = commands.add_parser("hash", help="the sender_hash a number appears under in logs")
    hashed.add_argument("phone")

    commands.add_parser("status", help="is the gateway's WhatsApp session connected?")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "hash":
        digest = sender_hash(settings, args.phone)
        if digest is None:
            _out("not an international number, e.g. +31612345678")
            return 2
        _out(f"sender_hash={digest}")
        return 0
    with httpx.Client(timeout=15.0) as client:
        if args.command == "simulate":
            return simulate(
                settings,
                url=args.url,
                from_=args.from_,
                text=args.text,
                replay=max(1, args.replay),
                client=client,
            )
        return status(settings, client=client)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
