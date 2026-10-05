"""``po-whatsapp`` -- operator tools for the WhatsApp channel.

``simulate``
    Sign and POST a Meta-shaped text message to a webhook URL, as Meta would. Exercises the
    whole path -- signature, ingress, queue, worker, reply attempt -- without a Meta app, so
    staging can be proved before the credentials exist (docs/runbook-iteration-02.md).
    Signs with ``WHATSAPP__APP_SECRET`` and addresses ``WHATSAPP__PHONE_NUMBER_ID`` from the
    environment, which must therefore match the target's.

``hash``
    Print the ``sender_hash`` a phone number appears under in the logs, given the target's
    ``LOGGING__PII_PEPPER``. The way to find one user's lines without logging their number.

``check``
    Call the Graph API with the configured token and number id: proves both are valid
    before any user is involved.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

import httpx

from personal_organizer.core.phone import normalise_e164
from personal_organizer.interfaces.channel import SenderRef
from personal_organizer.observability.redaction import hash_identifier
from personal_organizer.providers.channel.hmac_sha256 import SIGNATURE_HEADER, sign
from personal_organizer.providers.channel.whatsapp.parser import MESSAGES_FIELD, WEBHOOK_OBJECT
from personal_organizer.settings import Settings, get_settings


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def text_payload(
    *, phone_number_id: str, from_: str, text: str, wamid: str, timestamp: int
) -> dict[str, Any]:
    """A text message webhook in the shape Meta documents."""
    wa_id = from_.removeprefix("+")
    return {
        "object": WEBHOOK_OBJECT,
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": MESSAGES_FIELD,
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "0",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Simulated"}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": wamid,
                                    "timestamp": str(timestamp),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
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
    whatsapp = settings.whatsapp
    if whatsapp.app_secret is None or whatsapp.phone_number_id is None:
        _out("WHATSAPP__APP_SECRET and WHATSAPP__PHONE_NUMBER_ID must be set to sign a message")
        return 2
    phone = normalise_e164(from_)
    if phone is None:
        _out("--from must be an international number, e.g. +31612345678")
        return 2
    payload = text_payload(
        phone_number_id=whatsapp.phone_number_id,
        from_=phone,
        text=text,
        wamid=f"wamid.SIMULATED{uuid.uuid4().hex.upper()}",
        timestamp=int(time.time()),
    )
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {
        "content-type": "application/json",
        SIGNATURE_HEADER: sign(whatsapp.app_secret.get_secret_value().encode(), body),
    }
    failed = 0
    for attempt in range(1, replay + 1):
        response = client.post(url, content=body, headers=headers)
        _out(f"POST {attempt}/{replay} -> {response.status_code}")
        failed += response.status_code != httpx.codes.OK
    return 1 if failed else 0


def sender_hash(settings: Settings, phone: str) -> str | None:
    """The ``sender_hash`` log field for a phone-identified sender."""
    normalised = normalise_e164(phone)
    if normalised is None:
        return None
    key = SenderRef(user_id=None, phone=normalised).key
    return hash_identifier(key, settings.logging.pii_pepper.get_secret_value())


def check(settings: Settings, *, client: httpx.Client) -> int:
    whatsapp = settings.whatsapp
    if whatsapp.access_token is None or whatsapp.phone_number_id is None:
        _out("WHATSAPP__ACCESS_TOKEN and WHATSAPP__PHONE_NUMBER_ID must be set")
        return 2
    response = client.get(
        f"{whatsapp.graph_base_url.rstrip('/')}/{whatsapp.graph_api_version}/"
        f"{whatsapp.phone_number_id}",
        params={"fields": "display_phone_number,verified_name,quality_rating"},
        headers={"Authorization": f"Bearer {whatsapp.access_token.get_secret_value()}"},
    )
    if response.is_success:
        data = response.json()
        _out(
            f"ok: {data.get('verified_name')} ({data.get('display_phone_number')}), "
            f"quality {data.get('quality_rating')}"
        )
        return 0
    try:
        code = response.json()["error"]["code"]
    except ValueError, KeyError, TypeError:
        code = None
    _out(f"failed: HTTP {response.status_code}, Graph error code {code}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="po-whatsapp")
    commands = parser.add_subparsers(dest="command", required=True)

    sim = commands.add_parser("simulate", help="sign and POST a text message webhook")
    sim.add_argument("--url", required=True, help="e.g. http://localhost:8000/webhooks/whatsapp")
    sim.add_argument("--from", dest="from_", required=True, help="sender, E.164")
    sim.add_argument("--text", default="hello from po-whatsapp simulate")
    sim.add_argument("--replay", type=int, default=1, help="POST the identical body N times")

    hashed = commands.add_parser("hash", help="the sender_hash a number appears under in logs")
    hashed.add_argument("phone")

    commands.add_parser("check", help="verify the access token and phone number id")

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
        return check(settings, client=client)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
