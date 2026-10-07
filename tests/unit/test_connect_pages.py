"""The connect pages: D4's headers, a CSP that matches the page, escaping, direction."""

from __future__ import annotations

import base64
import hashlib
import html
import re

import markupsafe
import pytest
from fastapi import Response

from personal_organizer.api.pages import (
    CONTENT_SECURITY_POLICY,
    PAGE_CSS,
    SECURITY_HEADERS,
    connect_page,
    message_page,
    redirect,
)
from personal_organizer.onboarding.page_text import LANGUAGES, PAGE_TEXT, page_text

TOKEN = "eyJ0IjoiMSJ9.ZxY1aQ.c2lnbmF0dXJl"


def body(response: Response) -> str:
    return bytes(response.body).decode()


PAGES: dict[str, Response] = {
    "connect": connect_page(token=TOKEN, language="he", phone_suffix="4567"),
    "message": message_page("link_unusable", status_code=410),
    "redirect": redirect("https://connect.composio.test/link/ln_1"),
}


class TestHeaders:
    @pytest.mark.parametrize("name", sorted(PAGES))
    def test_every_response_carries_d4s_headers(self, name: str) -> None:
        for header, value in SECURITY_HEADERS.items():
            assert PAGES[name].headers[header] == value

    def test_the_headers_are_the_ones_d4_names(self) -> None:
        assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
        assert SECURITY_HEADERS["Cache-Control"] == "no-store"
        assert CONTENT_SECURITY_POLICY.startswith("default-src 'none'; ")
        assert "form-action 'self' https:" in CONTENT_SECURITY_POLICY
        assert "unsafe-inline" not in CONTENT_SECURITY_POLICY

    def test_the_redirect_is_a_303_to_the_url(self) -> None:
        assert PAGES["redirect"].status_code == 303
        assert PAGES["redirect"].headers["location"] == "https://connect.composio.test/link/ln_1"


class TestStyle:
    def test_the_csp_hash_matches_the_style_the_page_carries(self) -> None:
        """Browsers hash the exact text of the <style> element. Any drift and the page is
        unstyled, silently."""
        match = re.search(r"<style>(.*?)</style>", body(PAGES["connect"]), re.S)
        assert match is not None
        digest = base64.b64encode(hashlib.sha256(match.group(1).encode()).digest()).decode()
        assert f"style-src 'sha256-{digest}'" in CONTENT_SECURITY_POLICY

    def test_autoescaping_leaves_the_stylesheet_unchanged(self) -> None:
        assert str(markupsafe.escape(PAGE_CSS)) == PAGE_CSS


class TestContent:
    @pytest.mark.parametrize("name", ["connect", "message"])
    def test_nothing_is_loaded_from_anywhere(self, name: str) -> None:
        text = body(PAGES[name]).lower()
        for marker in ("<script", "<link", "<img", "src=", "http:", "https:", "@import"):
            assert marker not in text

    def test_the_button_posts_back_to_the_link(self) -> None:
        assert f'<form method="post" action="/connect/{TOKEN}">' in body(PAGES["connect"])

    def test_the_token_is_escaped(self) -> None:
        page = connect_page(token='"><script>alert(1)</script>', language="en", phone_suffix=None)
        assert "<script>" not in body(page)

    def test_hebrew_is_right_to_left(self) -> None:
        assert '<html lang="he" dir="rtl">' in body(PAGES["connect"])

    def test_english_is_left_to_right(self) -> None:
        page = connect_page(token=TOKEN, language="en", phone_suffix=None)
        assert '<html lang="en" dir="ltr">' in body(page)

    def test_an_unknown_language_gets_english(self) -> None:
        page = connect_page(token=TOKEN, language="fr", phone_suffix=None)
        assert '<html lang="en" dir="ltr">' in body(page)

    def test_the_last_digits_of_the_number_are_shown(self) -> None:
        """So someone handed another person's link sees it is not theirs."""
        assert "4567" in body(PAGES["connect"])

    def test_no_number_no_hint(self) -> None:
        page = connect_page(token=TOKEN, language="en", phone_suffix=None)
        assert page_text("connect", "en")["number_hint"].split("{")[0] not in html.unescape(
            body(page)
        )

    def test_a_page_for_nobody_in_particular_has_both_languages(self) -> None:
        text = html.unescape(body(PAGES["message"]))
        assert '<section lang="he" dir="rtl">' in text
        assert '<section lang="en" dir="ltr">' in text
        assert text.index(page_text("link_unusable", "he")["title"]) < text.index(
            page_text("link_unusable", "en")["title"]
        )

    def test_a_page_for_a_known_tenant_has_one(self) -> None:
        text = html.unescape(body(message_page("connected", status_code=200, language="en")))
        assert page_text("connected", "en")["title"] in text
        assert page_text("connected", "he")["title"] not in text


class TestCopy:
    @pytest.mark.parametrize("key", sorted(PAGE_TEXT))
    def test_every_page_has_both_languages_with_the_same_fields(self, key: str) -> None:
        assert set(PAGE_TEXT[key]) == set(LANGUAGES)
        assert PAGE_TEXT[key]["he"].keys() == PAGE_TEXT[key]["en"].keys()
        for language in LANGUAGES:
            assert all(value.strip() for value in PAGE_TEXT[key][language].values())
