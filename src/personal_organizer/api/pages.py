"""The connect pages: rendering, and the headers every response carries (D4).

- ``Referrer-Policy: no-referrer``: the page URL holds the link token, and the next hops are
  Composio and Google.
- ``Cache-Control: no-store``: on a shared phone, the back button must not bring back a page
  holding a live token.
- A CSP of ``default-src 'none'``, since the page loads nothing. Allowed back in: its one inline
  ``<style>``, by hash; ``form-action 'self' https:``, because Chrome checks ``form-action`` on
  every redirect of a form submission and ours redirects to Composio and on to Google; and
  nothing else. ``frame-ancestors 'none'`` and ``X-Frame-Options`` stop anyone framing the
  button.

Templates are autoescaped with ``StrictUndefined``. Copy is in
:mod:`personal_organizer.onboarding.page_text`.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Final

from fastapi import Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.status import HTTP_200_OK, HTTP_303_SEE_OTHER

from personal_organizer.onboarding.page_text import (
    BILINGUAL,
    page_language,
    page_text,
    text_direction,
)

#: The whole stylesheet. It has no quotes, angle brackets or ampersands, so autoescaping leaves
#: it byte-identical and the hash below is the hash of what the browser sees.
PAGE_CSS: Final = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;"
    "padding:0 1rem;line-height:1.5;color:#1a1a1a;background:#fff}"
    "h1{font-size:1.4rem}"
    "button{font-size:1.1rem;padding:.8rem 1.4rem;border:0;border-radius:.5rem;"
    "background:#1a73e8;color:#fff;width:100%}"
    "section+section{margin-top:2rem;border-top:1px solid #ddd;padding-top:1rem}"
)


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        f"style-src 'sha256-{_sha256(PAGE_CSS)}'",
        "form-action 'self' https:",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _html(template: str, status_code: int, **context: object) -> HTMLResponse:
    rendered = _templates.get_template(template).render(css=PAGE_CSS, **context)
    return HTMLResponse(rendered, status_code=status_code, headers=SECURITY_HEADERS)


def connect_page(*, token: str, language: str, phone_suffix: str | None) -> HTMLResponse:
    """The one-button page. ``phone_suffix`` names the number the link was issued to."""
    lang = page_language(language)
    text = page_text("connect", lang)
    hint = text["number_hint"].format(last_digits=phone_suffix) if phone_suffix else None
    return _html(
        "connect.html",
        HTTP_200_OK,
        lang=lang,
        direction=text_direction(lang),
        title=text["title"],
        text=text,
        token=token,
        number_hint=hint,
    )


def message_page(key: str, *, status_code: int, language: str | None = None) -> HTMLResponse:
    """A title and a line. With no ``language``, both languages, Hebrew first."""
    languages: tuple[str, ...] = BILINGUAL if language is None else (page_language(language),)
    sections = [
        {"lang": lang, "direction": text_direction(lang), **page_text(key, lang)}
        for lang in languages
    ]
    first = sections[0]
    return _html(
        "message.html",
        status_code,
        lang=first["lang"],
        direction=first["direction"],
        title=first["title"],
        sections=sections,
    )


def redirect(url: str) -> Response:
    """303 to ``url``; the policy headers on the redirect keep the token off the next hop."""
    return Response(status_code=HTTP_303_SEE_OTHER, headers={**SECURITY_HEADERS, "Location": url})


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "PAGE_CSS",
    "SECURITY_HEADERS",
    "connect_page",
    "message_page",
    "redirect",
]
