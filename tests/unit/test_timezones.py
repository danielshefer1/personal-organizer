from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from personal_organizer.messaging import timezones
from personal_organizer.messaging.timezones import ZONE_GUESSES, guess_zone, match_zone


class TestGuessZone:
    @pytest.mark.parametrize(
        ("phone", "zone"),
        [
            ("+972501234567", "Asia/Jerusalem"),
            ("+31612345678", "Europe/Amsterdam"),
            ("+447700900123", "Europe/London"),
            ("+4915112345678", "Europe/Berlin"),
            ("+33612345678", "Europe/Paris"),
            ("+353851234567", "Europe/Dublin"),
            ("+420601123456", "Europe/Prague"),  # longest prefix wins over a 2-digit one
            ("+40712345678", "Europe/Bucharest"),
        ],
    )
    def test_single_zone_country_codes(self, phone: str, zone: str) -> None:
        assert guess_zone(phone) == zone

    @pytest.mark.parametrize(
        "phone",
        [
            "+12025550123",  # +1 spans six zones
            "+79161234567",  # Russia, eleven
            "+34612345678",  # Spain, the Canaries are an hour behind
            "+5511912345678",  # Brazil
            "+61412345678",  # Australia
            None,
            "",
        ],
    )
    def test_no_guess_for_multi_zone_or_unknown_codes(self, phone: str | None) -> None:
        assert guess_zone(phone) is None

    def test_every_guess_is_a_real_zone(self) -> None:
        for zone in ZONE_GUESSES.values():
            ZoneInfo(zone)


class TestMatchZone:
    @pytest.mark.parametrize(
        ("text", "zone"),
        [
            ("Asia/Jerusalem", "Asia/Jerusalem"),
            ("asia/jerusalem", "Asia/Jerusalem"),
            (" Europe / London ", "Europe/London"),
            ("America/Indiana/Indianapolis", "America/Indiana/Indianapolis"),
            ("US/Eastern", "US/Eastern"),
            ("Jerusalem", "Asia/Jerusalem"),
            ("LONDON", "Europe/London"),
            ("London.", "Europe/London"),
            ("new york", "America/New_York"),
            ("New_York", "America/New_York"),
            ("New-York", "America/New_York"),
            ("port au prince", "America/Port-au-Prince"),
            ("Ho Chi Minh", "Asia/Ho_Chi_Minh"),
            ("sao paulo", "America/Sao_Paulo"),
            ("Kyiv", "Europe/Kyiv"),
            ("tel aviv", "Asia/Jerusalem"),
            ("Tel-Aviv", "Asia/Jerusalem"),
            ("israel", "Asia/Jerusalem"),
            ("Istanbul", "Europe/Istanbul"),
            ("Buenos Aires", "America/Argentina/Buenos_Aires"),
            ("utc", "UTC"),
            ("ירושלים", "Asia/Jerusalem"),
            ("תל אביב", "Asia/Jerusalem"),
            ("תל-אביב", "Asia/Jerusalem"),
            ("ישראל", "Asia/Jerusalem"),
            ("‏ירושלים", "Asia/Jerusalem"),
            ("לונדון", "Europe/London"),
        ],
    )
    def test_matches(self, text: str, zone: str) -> None:
        assert match_zone(text) == zone

    @pytest.mark.parametrize(
        "text",
        [
            "Mars",
            "",
            "   ",
            "1",
            "Europe",  # an area, not a zone
            "London, UK",  # deliberately small: no geocoding
            "Cordoba",  # America/Cordoba and America/Argentina/Cordoba
            "Louisville",  # America/Louisville and America/Kentucky/Louisville
            "Eastern",  # US/Eastern or Canada/Eastern: not offered by last segment
            "EST",  # a fixed offset with no DST, a trap for a New Yorker
            "GMT",  # a Londoner who types GMT means London, with summer time
            "Etc/GMT+3",  # the sign is inverted; nobody means it
            "x" * 65,
        ],
    )
    def test_unknown_ambiguous_or_trap_is_none(self, text: str) -> None:
        assert match_zone(text) is None

    def test_every_alias_is_a_real_zone(self) -> None:
        for zone in timezones._ALIASES.values():
            ZoneInfo(zone)
