"""The webhook parser: every documented shape, and garbage at every level.

The parser runs *after* signature verification, so an unexpected shape is Meta changing
something. It must store what it can, count what it cannot, and never raise -- an exception
answers 500 and Meta redelivers the same payload for seven days.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from personal_organizer.interfaces.channel import DeliveryStatus, SenderRef
from personal_organizer.providers.channel.whatsapp.parser import parse_webhook
from tests.fixtures.payloads import PHONE_NUMBER_ID, SENDER_PHONE, load, value_of


def _parse(payload: object) -> Any:
    return parse_webhook(payload, phone_number_id=PHONE_NUMBER_ID)


def _only_message(name: str) -> Any:
    batch = _parse(load(name))
    assert batch.skipped == 0
    (message,) = batch.messages
    return message


class TestMessageTypes:
    def test_text(self) -> None:
        message = _only_message("text")
        assert message.message_type == "text"
        assert message.text == "Oncology appointment with Dr Meyer"
        assert message.provider_message_id.startswith("wamid.")
        assert message.sent_at == datetime.fromtimestamp(1790000000, tz=UTC)

    def test_the_sender_is_normalised_to_e164(self) -> None:
        """Meta sends ``31612345678``; the allowlist holds ``+31612345678``."""
        message = _only_message("text")
        assert message.sender == SenderRef(user_id=None, phone=SENDER_PHONE)
        assert message.sender.key == f"tel:{SENDER_PHONE}"

    def test_audio_carries_its_media_id(self) -> None:
        message = _only_message("audio")
        assert message.message_type == "audio"
        assert message.media_id == "1003383421387256"
        assert message.media_mime_type == "audio/ogg; codecs=opus"
        assert message.text is None

    @pytest.mark.parametrize(
        ("name", "reply_id", "title"),
        [
            ("interactive_button", "confirm-yes", "Yes"),
            ("interactive_list", "slot-3", "Thursday 10:00"),
            ("template_button", "snooze-10", "Snooze"),
        ],
    )
    def test_replies_carry_the_chosen_id(self, name: str, reply_id: str, title: str) -> None:
        message = _only_message(name)
        assert message.reply_id == reply_id
        assert message.text == title
        assert message.context_message_id is not None

    def test_an_image_caption_is_its_text(self) -> None:
        message = _only_message("image")
        assert message.media_id == "1479537139650973"
        assert message.text == "my prescription"

    @pytest.mark.parametrize("name", ["reaction", "unsupported"])
    def test_other_types_are_kept_with_their_raw_object(self, name: str) -> None:
        """Stored, not dropped: ``raw`` lets a later iteration re-parse them."""
        message = _only_message(name)
        assert message.message_type == name
        assert message.raw["id"] == message.provider_message_id

    def test_a_batch_keeps_payload_order(self) -> None:
        batch = _parse(load("multi"))
        assert [m.text for m in batch.messages] == ["first", "second"]


class TestSenderIdentity:
    def test_a_bsuid_only_sender_is_kept(self) -> None:
        """A user behind a username has no phone number in the payload at all."""
        message = _only_message("bsuid_only")
        assert message.sender == SenderRef(user_id="US.13491208655302741918", phone=None)
        assert message.sender.key == "uid:US.13491208655302741918"

    def test_a_bsuid_wins_over_a_phone_for_the_identity_key(self) -> None:
        payload = load("text")
        value_of(payload)["messages"][0]["user_id"] = "US.1"
        message = _parse(payload).messages[0]
        assert message.sender.user_id == "US.1"
        assert message.sender.phone == SENDER_PHONE
        assert message.sender.key == "uid:US.1"

    def test_the_bsuid_can_come_from_the_contact(self) -> None:
        payload = load("text")
        value_of(payload)["contacts"][0]["user_id"] = "US.2"
        assert _parse(payload).messages[0].sender.user_id == "US.2"

    def test_a_non_numeric_from_under_an_unknown_field_name_is_kept_as_the_id(self) -> None:
        """If Meta names the BSUID field something BSUID_KEYS does not list, the message
        still arrives with an identity rather than being dropped."""
        payload = load("text")
        value = value_of(payload)
        value["contacts"] = []
        value["messages"][0]["from"] = "US.99"
        assert _parse(payload).messages[0].sender == SenderRef(user_id="US.99", phone=None)


class TestStatuses:
    def test_statuses_become_delivery_updates(self) -> None:
        batch = _parse(load("statuses"))
        assert batch.messages == ()
        assert [(u.provider_message_id, u.status) for u in batch.updates] == [
            ("wamid.OUT1", DeliveryStatus.SENT),
            ("wamid.OUT1", DeliveryStatus.DELIVERED),
            ("wamid.OUT1", DeliveryStatus.READ),
            ("wamid.OUT2", DeliveryStatus.FAILED),
        ]

    def test_a_failure_carries_its_error_code(self) -> None:
        failed = _parse(load("statuses")).updates[-1]
        assert failed.error_code == 131047

    def test_an_unknown_status_is_skipped(self) -> None:
        payload = load("statuses")
        value_of(payload)["statuses"] = [{"id": "wamid.X", "status": "deleted"}]
        batch = _parse(payload)
        assert batch.updates == ()
        assert batch.skipped == 1


class TestSkipped:
    def test_another_phone_number_is_skipped(self) -> None:
        """A WABA can hold several numbers, and the app hears about all of them."""
        batch = parse_webhook(load("text"), phone_number_id="999")
        assert batch.messages == ()
        assert batch.skipped == 1

    def test_an_unsubscribed_field_is_skipped(self) -> None:
        payload = load("text")
        payload["entry"][0]["changes"][0]["field"] = "account_update"
        assert _parse(payload).skipped == 1

    def test_the_wrong_object_is_skipped(self) -> None:
        payload = load("text")
        payload["object"] = "page"
        assert _parse(payload).messages == ()

    def test_a_message_without_an_id_is_skipped(self) -> None:
        """There is nothing to deduplicate it on, so it cannot be stored safely."""
        payload = load("text")
        del value_of(payload)["messages"][0]["id"]
        batch = _parse(payload)
        assert batch.messages == ()
        assert batch.skipped == 1

    def test_a_message_with_no_sender_at_all_is_skipped(self) -> None:
        payload = load("text")
        value_of(payload)["contacts"] = []
        del value_of(payload)["messages"][0]["from"]
        assert _parse(payload).skipped == 1

    def test_an_unreadable_timestamp_becomes_now(self) -> None:
        payload = load("text")
        value_of(payload)["messages"][0]["timestamp"] = "yesterday"
        sent_at = _parse(payload).messages[0].sent_at
        assert sent_at.tzinfo is not None
        assert abs((datetime.now(UTC) - sent_at).total_seconds()) < 60


def _garbage_variants() -> list[Any]:
    base = load("text")
    variants: list[Any] = [None, 1, "x", [], {}, {"object": "whatsapp_business_account"}]
    for path in (
        ("entry",),
        ("entry", 0),
        ("entry", 0, "changes"),
        ("entry", 0, "changes", 0),
        ("entry", 0, "changes", 0, "value"),
        ("entry", 0, "changes", 0, "value", "messages"),
        ("entry", 0, "changes", 0, "value", "messages", 0),
        ("entry", 0, "changes", 0, "value", "messages", 0, "text"),
        ("entry", 0, "changes", 0, "value", "contacts"),
    ):
        junk_values: tuple[Any, ...] = (None, 7, "s", [], {}, [None, 3])
        for junk in junk_values:
            payload = copy.deepcopy(base)
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = junk
            variants.append(payload)
    return variants


@pytest.mark.parametrize("payload", _garbage_variants())
def test_garbage_at_any_level_never_raises(payload: Any) -> None:
    batch = _parse(payload)
    assert isinstance(batch.messages, tuple)
