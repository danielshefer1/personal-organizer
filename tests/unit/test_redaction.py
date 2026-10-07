"""The redaction matrix.

This is the mechanised form of Iteration 01's "logs contain no raw PII".
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from personal_organizer.observability.redaction import (
    CONTENT_KEYS,
    FORBIDDEN_KEYS,
    MAX_STR,
    OPAQUE_ID_KEYS,
    SAFE_KEYS,
    SECRET_KEYS,
    Unredacted,
    hash_identifier,
    redact_event,
    scrub_text,
)
from tests.conftest import CONTENT_PII, PATTERN_PII


def _render(event: dict[str, Any], *, allow_raw: bool = False) -> str:
    return json.dumps(redact_event(event, pepper="pepper", allow_raw=allow_raw), default=str)


class TestAllowlistHygiene:
    def test_safe_and_forbidden_are_disjoint(self) -> None:
        """Guards against someone 'fixing' a redacted field by allowlisting it."""
        assert SAFE_KEYS.isdisjoint(FORBIDDEN_KEYS)

    def test_content_keys_are_not_safe(self) -> None:
        assert SAFE_KEYS.isdisjoint(CONTENT_KEYS)


class TestScrubText:
    @pytest.mark.parametrize("value", PATTERN_PII)
    def test_pattern_pii_is_removed(self, value: str) -> None:
        assert value not in scrub_text(f"inbound from {value} ok")

    def test_long_values_are_truncated(self) -> None:
        assert len(scrub_text("a" * 5000)) <= MAX_STR + len("...<truncated>")

    def test_ordinary_text_survives(self) -> None:
        assert scrub_text("deferred job to queue webhooks") == "deferred job to queue webhooks"

    def test_small_numbers_are_not_mangled(self) -> None:
        assert scrub_text("status 200 in 431 ms") == "status 200 in 431 ms"


class TestPositionalMatrix:
    """Every PII item, in every position it could realistically reach a log line."""

    @pytest.mark.parametrize("value", PATTERN_PII)
    @pytest.mark.parametrize("safe_key", ["event", "path", "route"])
    def test_pattern_pii_in_allowlisted_values(self, value: str, safe_key: str) -> None:
        assert value not in _render({safe_key: f"got {value}"})

    @pytest.mark.parametrize("value", PATTERN_PII + CONTENT_PII)
    def test_pii_under_unknown_key(self, value: str) -> None:
        assert value not in _render({"whatever": value})

    @pytest.mark.parametrize("value", PATTERN_PII + CONTENT_PII)
    def test_pii_under_content_key(self, value: str) -> None:
        assert value not in _render({"body": value, "message": value, "title": value})

    @pytest.mark.parametrize("value", PATTERN_PII)
    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_pattern_pii_nested_in_allowlisted_value(self, value: str, depth: int) -> None:
        nested: Any = value
        for _ in range(depth):
            nested = {"inner": nested}
        assert value not in _render({"exception": nested})

    @pytest.mark.parametrize("value", PATTERN_PII)
    def test_pattern_pii_in_list(self, value: str) -> None:
        assert value not in _render({"exception": [value, {"v": value}]})

    @pytest.mark.parametrize("value", PATTERN_PII + CONTENT_PII)
    def test_pii_in_nested_content_key(self, value: str) -> None:
        assert value not in _render({"exception": {"body": value}})


class TestCorrelationIdsSurvive:
    """The other half of the contract.

    ``request_id`` is allowlisted so a user's report can be tied to a log line, and
    ``tenant_id`` so a tenant's activity can be followed. Both are long alphanumeric
    strings, so the token and digit-run rules in scrub_text will rewrite them unless
    they are recognised as identifiers -- and a correlation id that never survives to
    stdout correlates nothing with nothing.
    """

    def test_opaque_keys_are_allowlisted(self) -> None:
        """A key that is not in SAFE_KEYS is shaped before it ever reaches the scrubber."""
        assert OPAQUE_ID_KEYS <= SAFE_KEYS

    def test_opaque_keys_are_never_forbidden(self) -> None:
        assert OPAQUE_ID_KEYS.isdisjoint(FORBIDDEN_KEYS)

    @pytest.mark.parametrize("key", sorted(OPAQUE_ID_KEYS))
    def test_uuid_hex_survives_under_every_opaque_key(self, key: str) -> None:
        value = uuid4().hex
        assert redact_event({key: value}, pepper="pepper")[key] == value

    def test_dashed_uuid_survives(self) -> None:
        value = str(uuid4())
        assert redact_event({"tenant_id": value}, pepper="pepper")["tenant_id"] == value

    def test_release_sha_survives(self) -> None:
        value = "9f2c1d4b" * 5  # 40 hex chars, as RAILWAY_GIT_COMMIT_SHA supplies
        assert redact_event({"release": value}, pepper="pepper")["release"] == value

    def test_digit_heavy_id_survives(self) -> None:
        """A hex id ending in a long digit run would otherwise trip the phone rule."""
        value = "ab" * 12 + "12345678"
        assert redact_event({"request_id": value}, pepper="pepper")["request_id"] == value

    @pytest.mark.parametrize("value", PATTERN_PII)
    def test_client_supplied_correlation_ids_are_still_scrubbed(self, value: str) -> None:
        """An allowlisted key is not a trusted value: request_id is taken from an inbound
        X-Request-ID header whenever the caller sends one."""
        assert value not in _render({"request_id": value})

    def test_all_digit_values_are_not_mistaken_for_identifiers(self) -> None:
        assert "0031612345678" not in _render({"request_id": "0031612345678"})


class TestSecrets:
    @pytest.mark.parametrize("key", sorted(SECRET_KEYS))
    def test_secret_keys_never_survive(self, key: str) -> None:
        assert "hunter2" not in _render({key: "hunter2"})

    def test_nested_secret_keys_never_survive(self) -> None:
        assert "hunter2" not in _render({"exception": {"password": "hunter2"}})


class TestHashing:
    def test_identifier_is_replaced_by_a_hash(self) -> None:
        out = redact_event({"phone": "+31612345678"}, pepper="pepper")
        assert "phone" not in out
        assert out["phone_hash"] == hash_identifier("+31612345678", "pepper")

    def test_hash_is_stable_and_correlatable(self) -> None:
        a = redact_event({"wa_id": "31612345678"}, pepper="p")["wa_id_hash"]
        b = redact_event({"wa_id": " 31612345678 "}, pepper="p")["wa_id_hash"]
        assert a == b

    def test_pepper_changes_the_hash(self) -> None:
        assert hash_identifier("x", "p1") != hash_identifier("x", "p2")

    @pytest.mark.parametrize("key", ["wamid", "provider_message_id"])
    def test_whatsapp_message_ids_are_hashed_not_logged(self, key: str) -> None:
        """A wamid is base64 over a structure embedding the counterpart's number."""
        wamid = "wamid.HBgLMzE2MTIzNDU2NzgVAgASGBQzQTdEMEY1QjQ1RjE4NjhBMUUwRQA="
        out = redact_event({key: wamid}, pepper="p")
        assert key not in out
        assert out[f"{key}_hash"] == hash_identifier(wamid, "p")


class TestMessagingKeys:
    def test_inbox_row_ids_survive_as_correlation_handles(self) -> None:
        inbox_id, outbox_id = str(uuid4()), str(uuid4())
        out = redact_event({"inbox_id": inbox_id, "outbox_id": outbox_id}, pepper="p")
        assert out == {"inbox_id": inbox_id, "outbox_id": outbox_id}

    def test_a_webhook_verify_token_in_a_url_is_scrubbed(self) -> None:
        """The settings floor of 32 characters is what makes this true."""
        token = "a3f1" * 16
        url = f"/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token={token}&hub.challenge=1"
        assert token not in scrub_text(url)


class TestUnredactedEscapeHatch:
    def test_ignored_by_default(self) -> None:
        assert "+31612345678" not in _render({"note": Unredacted("+31612345678")})

    def test_honoured_when_explicitly_allowed(self) -> None:
        assert "+31612345678" in _render({"note": Unredacted("+31612345678")}, allow_raw=True)


class TestConnectUrls:
    """Onboarding links carry a bearer token in their path, and Composio's callback our signed
    state in its query. Both go by position, whatever the token's length or shape."""

    @pytest.mark.parametrize(
        "token",
        ["abc.def.ghi", "eyJ0IjoiMSJ9.ZxY1aQ.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU"],
    )
    def test_a_link_token_is_removed(self, token: str) -> None:
        assert (
            scrub_text(f"GET https://po.test/connect/{token} 410")
            == "GET https://po.test/connect/<redacted:link> 410"
        )

    def test_the_state_is_removed_and_the_account_id_kept(self) -> None:
        assert (
            scrub_text("/connect/callback?state=abc.def.ghi&connected_account_id=ca_1")
            == "/connect/callback?state=<redacted:state>&connected_account_id=ca_1"
        )

    @pytest.mark.parametrize(
        "value", ["/connect/{token}", "/connect/callback", "/webhooks/gowa", "disconnect/now"]
    )
    def test_route_templates_and_other_paths_survive(self, value: str) -> None:
        assert scrub_text(value) == value

    def test_a_logged_path_is_scrubbed(self) -> None:
        rendered = _render({"event": "request.invalid", "path": "/connect/abc.def.ghi"})
        assert "abc.def.ghi" not in rendered

    def test_a_connection_id_survives_for_correlation(self) -> None:
        connection_id = str(uuid4())
        assert connection_id in _render(
            {"event": "connect.connected", "connection_id": connection_id}
        )
