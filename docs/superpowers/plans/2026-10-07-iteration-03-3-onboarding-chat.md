# Iteration 03-lite, PR 3 (`iteration-03/3-onboarding-chat`): Onboarding Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An invited number that writes to the bot becomes a tenant, confirms its time zone with a numbered reply (or a city), and is sent a signed connect link. A known tenant's message content moves under RLS. All of it is idempotent under job re-runs.

**Architecture:** `handle_inbound` keeps its explicit-argument style and gains one keyword, `settings`. With `settings.composio.enabled` it runs the D2 tenant gate. Otherwise it runs Iteration 02's allowlist gate unchanged. The gate resolves the sender through the network-keyed identities (D12) and enrols invited newcomers. It hands `onboarding` tenants to a step machine (`messaging/onboarding.py`). The step machine **sends first and returns the state change**. The gate then commits that state change together with D7 (copy into `messages`, null the inbox content) and `processed_at`, in one tenant transaction. A re-run before that commit reaches the same decision, its sends are already claimed by `send_once`, and the step advances once. Choices (D9), language (D11), time zones (D10), the text table and link tokens are small, pure modules with exhaustive unit tests.

**Tech Stack:** Python 3.14, uv, SQLAlchemy 2 async + asyncpg, PostgreSQL 16 with RLS, Procrastinate, itsdangerous, zoneinfo + tzdata, pytest (+ pytest-asyncio auto mode), ruff, mypy strict.

**Spec:** `docs/plan-iteration-03.md`. This PR covers D2, D6, D7, D9, D10 (`zone` step and the sending of the link), D11 and D12. The binding cross-PR names are in `docs/superpowers/plans/2026-10-07-iteration-03-contract.md`. This PR is stacked on `iteration-03/2-schema`. Treat everything under "PR 2 — produces" in the contract as existing.

## Contract deviations

None. Everything the contract lists for PR 3 keeps its name, module and signature. This plan adds some things the contract leaves open, all of them additive:

- `handle_inbound(..., settings: Settings | None = None, ...)`. The contract says onboarding runs only when `settings.composio.enabled` but does not say how the settings arrive. An explicit keyword keeps the function testable without patching. `None` (every existing caller and test) means Iteration 02 exactly.
- `add_identity(session, tenant_id, *, network, external_id, phone) -> bool` (`True` if a row was inserted) is appended to PR 2's `db/repositories/tenants.py`. D12 says the worker adds a `uid:` row for a phone-keyed tenant, and no PR 2 function inserts an identity for an existing tenant.
- `InboxRow` moves to the new module `messaging/inbox.py`, together with a public `load_row`. `messaging/inbound.py` re-exports `InboxRow` and keeps it in `__all__`, so `tests/db/test_handle_inbound.py` is untouched. `InboxRow` gains `sender_user_id` and `reply_id`.
- `enrol` creates the tenant on the `tel:` key when the sender has a phone (the strongest key otherwise) and links all missing keys (controller ruling, Task 7), so racing first messages on two channels meet at one `create_tenant` call.
- D12's "`resolve_tenant` tries `uid:` first, then `tel:`" is done by the caller (`messaging/tenancy.py`). The contract's SQL function takes one `external_id`, so the worker calls it once per key, strongest first.

## Global Constraints

- Python `>=3.14`. Use `uv run` for everything. ruff line length 100. **mypy strict covers `tests/` too**, so every test function and helper is annotated (`-> None`, typed params).
- Every module starts with a dense explanatory docstring, ends with `__all__`, and uses `Final` for constants. Imports are absolute only (`ban-relative-imports = "all"`). Logging goes through `structlog.get_logger(__name__)`, never `logging.getLogger`.
- Logs carry only redaction-allowlisted keys (`inbox_id`, `tenant_id`, `channel`, `disposition`, `reason`, `message_type`, `error_type`, `sender` → hashed). Never log a phone, a body, a link URL or a token.
- **`OutboundChannel` does not change.** A choice is sent as plain text through `send_once` (D9).
- A choice has 1–9 options, so every numbered reply is a single digit (D9).
- Hebrew detection: any char in U+0590–U+05FF means `"he"`, otherwise `"en"` (D11).
- `tenants.timezone` is written only when the user confirms. The guess is never stored (D10).
- `tenant_identities.network` is `"whatsapp"` (`NETWORK_WHATSAPP`) for both the `gowa` and the `whatsapp` channel. `external_id` is the `SenderRef.key` (`tel:+…` / `uid:…`) (D12).
- Onboarding runs only when `settings.composio.enabled`. When it is off, `handle_inbound` behaves exactly as in Iteration 02 (contract).
- `send_once`: exactly one of `inbox_id` / `idempotency_key` is not None, or `ValueError`. Every ADR 0003 guarantee holds for both claim paths. The outbox stores no message text (ADR 0003).
- Onboarding outbox kinds are exactly `"onboarding:welcome_zone"`, `"onboarding:zone_ask_city"`, `"onboarding:zone_retry"`, `"onboarding:connect"` and `"onboarding:connect_resend"` (contract).
- Text keys used across PRs: `"connect_link"` (value `url`) and `"all_set"` (contract).
- `tests/db/test_handle_inbound.py` stays green and unmodified.
- DB tests need the local database: `docker compose up -d`, `uv run po-db bootstrap`, `uv run alembic upgrade head`. Without it they skip. A skip is not a pass, so run them with the database up.

## Review Focus

These are the input classes and failure modes most likely to bite a real person. The spec implies them, but its own Done-When tests would not catch them. Each one has a test in the owning task.

1. **A job that dies after the connect link is sent but before the step is committed** (deploy, lost DB connection). The person must get exactly one link message, and the step must move to `connect` once. A naive "write state first" re-run would send `connect_resend` as a second link. → Task 9, `test_a_crash_after_the_send_neither_resends_nor_advances_twice`.
2. **A reused link with seconds left to live.** The person taps it after switching apps and it has expired. A link is reused only while at least half its TTL remains. → Task 6, `test_a_link_past_half_its_life_is_replaced`.
3. **One person, two keys.** They first write via Meta (BSUID + phone), later via the GOWA number (phone only), or the other way round. They must stay one tenant, and a later BSUID-only message (hidden number) must still resolve. → Task 7 `TestIdentities` and Task 9 `TestOnePersonTwoChannels`.
4. **The link URL, or its token, in the logs.** It is a bearer credential for the connect page. → Task 9, `test_onboarding_logs_neither_who_nor_what_nor_the_link`.
5. **A voice note, sticker or image during the `zone` step** (no text, no `reply_id`). It must get the retry prompt, not a crash and not silence. → Task 8, `test_a_message_without_text_gets_the_retry_prompt`.

---

## File map

| File | Status | Responsibility |
|---|---|---|
| `src/personal_organizer/messaging/language.py` | create | `detect_language` (D11), `strip_bidi` |
| `src/personal_organizer/messaging/choices.py` | create | `Option`, `Choice`, `render_text`, `resolve` (D9) |
| `src/personal_organizer/messaging/timezones.py` | create | `ZONE_GUESSES`, `guess_zone`, `match_zone` (D10) |
| `src/personal_organizer/messaging/onboarding_text.py` | create | `ONBOARDING_TEXT`, `t` (D11) |
| `src/personal_organizer/onboarding/__init__.py` | create | package docstring |
| `src/personal_organizer/onboarding/tokens.py` | create | `TokenPayload`, `sign`, `verify`, salts |
| `src/personal_organizer/onboarding/links.py` | create | `issue_or_reuse_link` |
| `src/personal_organizer/messaging/outbox.py` | modify | `send_once(..., idempotency_key=)` (D6) |
| `src/personal_organizer/messaging/inbox.py` | create | `InboxRow` (moved, +2 fields), `load_row` |
| `src/personal_organizer/messaging/tenancy.py` | create | `network_for`, `identity_keys`, `resolve_sender`, `enrol`, `TenantState`, `load_state`, `phone_of` |
| `src/personal_organizer/db/repositories/tenants.py` | modify (PR 2's) | `+ add_identity` |
| `src/personal_organizer/messaging/onboarding.py` | create | step machine: `Advance`, `zone_choice`, texts, `onboarding_step` |
| `src/personal_organizer/messaging/inbound.py` | modify | D2 tenant gate, D7 finishing, `settings` keyword |
| `src/personal_organizer/worker/tasks/channel.py` | modify | pass `settings=get_settings()` |
| `pyproject.toml`, `uv.lock` | modify | `tzdata` as a direct dependency |
| `docs/adr/0003-replies-are-at-most-once-on-ambiguity.md` | modify | one Consequences bullet for the key |
| `tests/fixtures/channels.py` | create | `FakeOutbound`, `Spy`, `insert_inbox` |
| `tests/fixtures/tenants.py` | create | onboarding settings, phones, tenant helpers |
| `tests/db/conftest.py` | modify | `clean_tenant_tables`, `onboarding_settings` |
| `tests/unit/test_language.py`, `test_choices.py`, `test_timezones.py`, `test_onboarding_text.py`, `test_onboarding_tokens.py`, `test_tenancy.py` | create | unit tests |
| `tests/db/test_outbox.py`, `test_onboarding_links.py`, `test_tenancy.py`, `test_onboarding_steps.py`, `test_onboarding.py` | create | DB tests |

---

### Task 1: Language helpers and the channel-neutral choice (D9, D11)

**Files:**
- Create: `src/personal_organizer/messaging/language.py`
- Create: `src/personal_organizer/messaging/choices.py`
- Test: `tests/unit/test_language.py`
- Test: `tests/unit/test_choices.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `detect_language(text: str | None) -> str` returns `"he"` or `"en"`.
  - `strip_bidi(text: str) -> str` removes Unicode directional marks, which Hebrew keyboards insert invisibly.
  - `Option(id: str, label: str, aliases: tuple[str, ...] = ())` and `Choice(prompt: str, options: tuple[Option, ...])`, both frozen and slotted, with 1–9 options and distinct ids.
  - `MAX_OPTIONS: Final = 9`
  - `render_text(choice: Choice, language: str) -> str`
  - `resolve(choice: Choice, *, reply_id: str | None, text: str | None) -> Option | None`

- [ ] **Step 1: Write the failing language tests**

`tests/unit/test_language.py`:

```python
from __future__ import annotations

import pytest

from personal_organizer.messaging.language import detect_language, strip_bidi


class TestDetectLanguage:
    @pytest.mark.parametrize(
        "text",
        ["שלום", "hi שלום", "מה נשמע?", "֐", "׿", "Meeting at 10 בבוקר"],
    )
    def test_any_hebrew_letter_means_hebrew(self, text: str) -> None:
        assert detect_language(text) == "he"

    @pytest.mark.parametrize(
        "text",
        [None, "", "   ", "hello", "Grüße", "مرحبا", "֏", "؀", "123", "👍"],
    )
    def test_anything_else_is_english(self, text: str | None) -> None:
        assert detect_language(text) == "en"


class TestStripBidi:
    def test_removes_the_marks_hebrew_keyboards_insert(self) -> None:
        assert strip_bidi("‏כן‎") == "כן"
        assert strip_bidi("a⁧b⁩c‫d‬") == "abcd"

    def test_leaves_everything_else_alone(self) -> None:
        assert strip_bidi(" Tel Aviv-Yafo 1. ") == " Tel Aviv-Yafo 1. "
```

- [ ] **Step 2: Write the failing choice tests**

`tests/unit/test_choices.py`:

```python
"""Done-When: "a choice works on every channel". A numbered reply, a label or alias reply, a
Meta button's ``reply_id`` and a GOWA selection's ``selected_id`` all resolve the same way."""

from __future__ import annotations

import pytest

from personal_organizer.messaging.choices import (
    MAX_OPTIONS,
    Choice,
    Option,
    render_text,
    resolve,
)

YES = Option("zone:correct", "Correct", ("yes", "כן"))
NO = Option("zone:change", "Change", ("no", "לא"))
CHOICE = Choice("Is your time zone Asia/Jerusalem?", (YES, NO))


def _options(count: int) -> tuple[Option, ...]:
    return tuple(Option(f"o{n}", f"Option {n}") for n in range(1, count + 1))


class TestNumberedReplies:
    @pytest.mark.parametrize("text", ["1", "1.", "1)", " 1 ", "\t1.\n", "1 )", "‏1"])
    def test_the_first_option(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is YES

    @pytest.mark.parametrize("text", ["2", "2.", " 2) "])
    def test_the_second_option(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is NO

    @pytest.mark.parametrize("text", ["3", "9", "0", "12", "1.5", "-1", "#1", "1).", "one"])
    def test_out_of_range_or_not_a_single_digit(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is None


class TestWordReplies:
    @pytest.mark.parametrize(
        "text", ["Correct", "correct", "CORRECT", "  correct  ", "Yes!", "yes", "YES.", "כן", "‏כן!"]
    )
    def test_the_label_or_an_alias_case_folded(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is YES

    @pytest.mark.parametrize("text", ["Change", "no", "NO!", "לא"])
    def test_the_other_option_by_word(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is NO

    def test_a_hebrew_label(self) -> None:
        right, change = Option("a", "נכון"), Option("b", "לשנות")
        choice = Choice("האם אזור הזמן שלך הוא Asia/Jerusalem?", (right, change))
        assert resolve(choice, reply_id=None, text="‏נכון!") is right
        assert resolve(choice, reply_id=None, text="לשנות") is change

    def test_casefold_not_lower(self) -> None:
        street = Option("s", "Straße")
        assert resolve(Choice("Where?", (street,)), reply_id=None, text="STRASSE") is street

    @pytest.mark.parametrize("text", [None, "", "   ", "maybe", "yes please", "Corrected"])
    def test_anything_else_is_none(self, text: str | None) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is None


class TestReplyIds:
    def test_a_meta_button(self) -> None:
        assert resolve(CHOICE, reply_id="zone:change", text="Change") is NO

    def test_a_gowa_selection(self) -> None:
        # GOWA passes the row's own text alongside ``selected_id``; the id decides.
        assert resolve(CHOICE, reply_id="zone:correct", text="1. Correct") is YES

    def test_the_id_wins_over_the_text(self) -> None:
        assert resolve(CHOICE, reply_id="zone:change", text="1") is NO

    def test_an_unknown_id_falls_back_to_the_text(self) -> None:
        assert resolve(CHOICE, reply_id="stale-button", text="2") is NO

    def test_an_unknown_id_and_unknown_text(self) -> None:
        assert resolve(CHOICE, reply_id="stale-button", text="hmm") is None


class TestValidation:
    @pytest.mark.parametrize("count", [0, MAX_OPTIONS + 1])
    def test_one_to_nine_options(self, count: int) -> None:
        with pytest.raises(ValueError, match="1 to 9"):
            Choice("?", _options(count))

    @pytest.mark.parametrize("count", [1, MAX_OPTIONS])
    def test_the_bounds_are_allowed(self, count: int) -> None:
        assert len(Choice("?", _options(count)).options) == count

    def test_option_ids_are_distinct(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            Choice("?", (Option("x", "A"), Option("x", "B")))


class TestRenderText:
    def test_english(self) -> None:
        assert render_text(CHOICE, "en") == (
            "Is your time zone Asia/Jerusalem?\n\n1. Correct\n2. Change\n\nReply with a number."
        )

    def test_hebrew(self) -> None:
        choice = Choice(
            "האם אזור הזמן שלך הוא Asia/Jerusalem?", (Option("a", "נכון"), Option("b", "לשנות"))
        )
        assert render_text(choice, "he") == (
            "האם אזור הזמן שלך הוא Asia/Jerusalem?\n\n1. נכון\n2. לשנות\n\nנא להשיב במספר."
        )

    def test_an_unknown_language_gets_the_english_hint(self) -> None:
        assert render_text(CHOICE, "fr").endswith("Reply with a number.")

    @pytest.mark.parametrize("count", range(1, MAX_OPTIONS + 1))
    def test_every_rendered_number_resolves_to_its_option(self, count: int) -> None:
        choice = Choice("?", _options(count))
        rendered = render_text(choice, "en")
        for number, option in enumerate(choice.options, start=1):
            assert f"{number}. {option.label}" in rendered
            assert resolve(choice, reply_id=None, text=str(number)) is option
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `uv run pytest tests/unit/test_language.py tests/unit/test_choices.py -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'personal_organizer.messaging.language'` (and `.choices`).

- [ ] **Step 4: Implement `language.py`**

`src/personal_organizer/messaging/language.py`:

```python
"""The tenant's language, detected rather than asked (D11).

Onboarding has no language step: the first message decides. Any Hebrew letter (U+0590 to
U+05FF) means Hebrew, otherwise English. A one-word "שלום" is unmistakable, and a mixed
"hi שלום" comes from someone who reads Hebrew. The result is stored on the tenant, and the
agent can change it later. Two languages, because that is who the circle is. A third is a
table column in ``onboarding_text``, not a new mechanism.

Also here: :func:`strip_bidi`. Hebrew keyboards and some WhatsApp clients insert invisible
directional marks around words and digits. Unremoved, "\\u200f1" is not "1" and
"\\u200fכן" is not "כן". Every parser of user replies strips them first.
"""

from __future__ import annotations

from typing import Final

HEBREW: Final = "he"
ENGLISH: Final = "en"

_HEBREW_FIRST: Final = "֐"
_HEBREW_LAST: Final = "׿"

#: LRM, RLM, the embeddings and overrides, and the isolates.
_BIDI_MARKS: Final = str.maketrans(
    "", "", "‎‏‪‫‬‭‮⁦⁧⁨⁩"
)


def detect_language(text: str | None) -> str:
    """``"he"`` if ``text`` holds any Hebrew-block character, else ``"en"``."""
    if text and any(_HEBREW_FIRST <= char <= _HEBREW_LAST for char in text):
        return HEBREW
    return ENGLISH


def strip_bidi(text: str) -> str:
    """``text`` without Unicode directional marks, which change no meaning but break matching."""
    return text.translate(_BIDI_MARKS)


__all__ = ["ENGLISH", "HEBREW", "detect_language", "strip_bidi"]
```

- [ ] **Step 5: Implement `choices.py`**

`src/personal_organizer/messaging/choices.py`:

```python
"""A channel-neutral choice, rendered as numbered text (D9).

Linked-device WhatsApp cannot be relied on to show buttons. Later steps depend on choices,
starting with the Confirm step for calendar writes. So a choice is a messaging concept, not a
channel feature. It goes out as plain text through ``send_once``, and ``OutboundChannel`` does
not change. Native rendering (Meta buttons, GOWA polls) is a later ``send_choice`` on the
channels that can do it. :func:`resolve` already accepts what those send back: the picked
option's id arrives as the inbox row's ``reply_id``.

**The caller owns which choice is open.** That is ``tenants.onboarding_step`` now and
``pending_actions`` later. The caller rebuilds the same :class:`Choice` from that state on
every message. No choice is ever stored, so there is nothing to expire and nothing that can
disagree with the state. A reply that resolves to nothing returns ``None``, and the caller
decides between re-prompting and treating it as free text.

At most nine options, so every numbered reply is a single digit and "12" can never be read
as "1" plus noise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import ENGLISH, strip_bidi

MAX_OPTIONS: Final = 9

#: "1", "1." or "1)", with surrounding whitespace ignored. Nothing else counts as a number.
_NUMBERED: Final = re.compile(r"\s*([1-9])\s*[.)]?\s*")

#: Edges that decorate a word reply without changing it: "Yes!", "ok.", "'no'".
_EDGES: Final = " \t\r\n.!?,;:'\""

_HINTS: Final = MappingProxyType({"en": "Reply with a number.", "he": "נא להשיב במספר."})


@dataclass(frozen=True, slots=True)
class Option:
    #: Stable and channel-neutral. A native button or list row carries it as its id.
    id: str
    #: What the numbered line shows, in the tenant's language.
    label: str
    #: Other words that mean this option, matched case-folded. Callers list both languages,
    #: because a Hebrew reader may answer an English prompt in Hebrew and the other way round.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Choice:
    prompt: str
    options: tuple[Option, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.options) <= MAX_OPTIONS:
            msg = f"a choice has 1 to {MAX_OPTIONS} options, not {len(self.options)}"
            raise ValueError(msg)
        ids = [option.id for option in self.options]
        if len(set(ids)) != len(ids):
            msg = "option ids must be distinct"
            raise ValueError(msg)


def _fold(text: str) -> str:
    return " ".join(text.strip(_EDGES).split()).casefold()


def render_text(choice: Choice, language: str) -> str:
    """The prompt, one numbered line per option, and "Reply with a number" (or its Hebrew)."""
    numbered = [f"{number}. {option.label}" for number, option in enumerate(choice.options, 1)]
    hint = _HINTS.get(language, _HINTS[ENGLISH])
    return "\n".join([choice.prompt, "", *numbered, "", hint])


def resolve(choice: Choice, *, reply_id: str | None, text: str | None) -> Option | None:
    """The option a reply picks, or ``None``.

    Tried in order: ``reply_id`` equal to an option id (a Meta button or a GOWA selection);
    then the text as a single digit in range; then the text as a label or alias, case-folded.
    An unknown ``reply_id``, from a button of an older message, falls through to the text.
    """
    if reply_id is not None:
        for option in choice.options:
            if option.id == reply_id:
                return option
    if text is None:
        return None
    text = strip_bidi(text)
    if (numbered := _NUMBERED.fullmatch(text)) is not None:
        index = int(numbered.group(1)) - 1
        return choice.options[index] if index < len(choice.options) else None
    wanted = _fold(text)
    if not wanted:
        return None
    for option in choice.options:
        if wanted == _fold(option.label) or any(wanted == _fold(a) for a in option.aliases):
            return option
    return None


__all__ = ["MAX_OPTIONS", "Choice", "Option", "render_text", "resolve"]
```

- [ ] **Step 6: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_language.py tests/unit/test_choices.py -q`
Expected: all pass.

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: no errors. (`ruff format` rewrites files in place, so review its diff before committing.)

- [ ] **Step 8: Commit**

```bash
git add src/personal_organizer/messaging/language.py src/personal_organizer/messaging/choices.py \
        tests/unit/test_language.py tests/unit/test_choices.py
git commit -m "feat: channel-neutral choices as numbered text, and language detection"
```

---

### Task 2: Time zones, guessed and matched (D10)

**Files:**
- Create: `src/personal_organizer/messaging/timezones.py`
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)
- Test: `tests/unit/test_timezones.py`

**Interfaces:**
- Consumes: `strip_bidi` (Task 1).
- Produces:
  - `ZONE_GUESSES: Final[Mapping[str, str]]` maps a country calling code (`"+972"`) to a zone.
  - `guess_zone(phone: str | None) -> str | None`
  - `match_zone(text: str) -> str | None` returns a name from `zoneinfo.available_timezones()` (or `"UTC"`), or `None` when the text is unknown or ambiguous.

- [ ] **Step 1: Make `tzdata` a direct dependency**

`zoneinfo` reads the system database, and falls back to the `tzdata` package. This machine has no `/usr/share/zoneinfo`, and `tzdata` arrives today only as a transitive dependency of `icalendar`. Matching cities depends on it, so declare it.

Run: `uv add "tzdata>=2026.4"`
Expected: `pyproject.toml` `dependencies` gains `"tzdata>=2026.4"`, and `uv.lock` updates.

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_timezones.py`:

```python
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
```

- [ ] **Step 3: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_timezones.py -q`
Expected: `ModuleNotFoundError: No module named 'personal_organizer.messaging.timezones'`.

- [ ] **Step 4: Implement**

`src/personal_organizer/messaging/timezones.py`:

```python
"""Time zones for onboarding: a guess from the number, and a match for what the user types (D10).

**The guess** comes from the country calling code, and only for codes that span exactly one
zone: ``+972``, the UK, Ireland and the common EU codes. Multi-zone codes (``+1``, ``+7``,
``+34`` with the Canaries, ``+351`` with the Azores, ``+55``, ``+61``) get no guess, and the
user is asked for a city. The guess is computed whenever the step is rendered and **never
stored**. ``tenants.timezone`` is written only when the user confirms.

**The match** is deliberately small: no geocoding. Tried in order:

1. A short alias table: Hebrew names, plus English names the IANA table spells another
   way or more than one way ("tel aviv", "istanbul", "buenos aires", "utc").
2. A full IANA name, case-insensitively ("europe/london"). Names with no area (``EST``,
   ``GB``) and ``Etc/*`` are not offered. They are fixed offsets or sign-inverted, which
   is the opposite of what a person typing them means.
3. The last segment of a zone in a geographic area ("London", "new york"), but only when
   exactly one zone has it. "Cordoba" names two zones and matches neither.

Spaces, underscores and hyphens are interchangeable ("new york", "New_York", "New-York"),
directional marks are ignored, and edge punctuation is dropped. Anything else is ``None``,
and the caller re-prompts with an example.
"""

from __future__ import annotations

import functools
import re
import zoneinfo
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import strip_bidi

#: Country calling code -> its single zone. Longest prefix wins (``+420`` before ``+40``).
ZONE_GUESSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "+972": "Asia/Jerusalem",
        "+44": "Europe/London",
        "+353": "Europe/Dublin",
        "+31": "Europe/Amsterdam",
        "+32": "Europe/Brussels",
        "+33": "Europe/Paris",
        "+39": "Europe/Rome",
        "+41": "Europe/Zurich",
        "+43": "Europe/Vienna",
        "+45": "Europe/Copenhagen",
        "+46": "Europe/Stockholm",
        "+47": "Europe/Oslo",
        "+48": "Europe/Warsaw",
        "+49": "Europe/Berlin",
        "+30": "Europe/Athens",
        "+36": "Europe/Budapest",
        "+40": "Europe/Bucharest",
        "+358": "Europe/Helsinki",
        "+420": "Europe/Prague",
    }
)
_PREFIXES: Final = tuple(sorted(ZONE_GUESSES, key=len, reverse=True))

_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ירושלים": "Asia/Jerusalem",
        "תל אביב": "Asia/Jerusalem",
        "תל אביב יפו": "Asia/Jerusalem",
        "ישראל": "Asia/Jerusalem",
        "חיפה": "Asia/Jerusalem",
        "באר שבע": "Asia/Jerusalem",
        "אילת": "Asia/Jerusalem",
        "לונדון": "Europe/London",
        "פריז": "Europe/Paris",
        "ברלין": "Europe/Berlin",
        "אמסטרדם": "Europe/Amsterdam",
        "ניו יורק": "America/New_York",
        # IANA's own "Asia/Tel_Aviv" is a legacy link; store the canonical name.
        "tel aviv": "Asia/Jerusalem",
        "tel aviv yafo": "Asia/Jerusalem",
        "israel": "Asia/Jerusalem",
        # Two zones share these last segments; both are the same clock.
        "istanbul": "Europe/Istanbul",
        "nicosia": "Asia/Nicosia",
        "buenos aires": "America/Argentina/Buenos_Aires",
        "utc": "UTC",
    }
)

#: Areas whose zones are places. ``US/``, ``Canada/``, ``Brazil/`` etc. are legacy links that
#: would make "Eastern" or "Pacific" ambiguous, so they match by full name only.
_GEOGRAPHIC_AREAS: Final = frozenset(
    {"Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe",
     "Indian", "Pacific"}
)

#: A city or a zone name. Longer input is a sentence, not an answer.
_MAX_INPUT: Final = 64
_EDGES: Final = " \t\r\n.!?,;:'\""
_SLASH: Final = re.compile(r"\s*/\s*")


def _key(text: str) -> str:
    text = _SLASH.sub("/", strip_bidi(text).strip(_EDGES))
    return "_".join(text.replace("-", " ").replace("_", " ").split()).casefold()


@functools.cache
def _index() -> tuple[dict[str, str], dict[str, str], dict[str, str | None]]:
    """(aliases, full names, unique last segments). ``None`` marks an ambiguous segment."""
    aliases = {_key(name): zone for name, zone in _ALIASES.items()}
    full: dict[str, str] = {}
    last: dict[str, str | None] = {}
    for name in zoneinfo.available_timezones():
        if "/" not in name or name.startswith("Etc/"):
            continue
        full[_key(name)] = name
        area, _, rest = name.partition("/")
        if area in _GEOGRAPHIC_AREAS:
            segment = _key(rest.rsplit("/", 1)[-1])
            last[segment] = name if last.get(segment, name) == name else None
    return aliases, full, last


def guess_zone(phone: str | None) -> str | None:
    """The zone of an E.164 number's country, if that country has exactly one."""
    if not phone:
        return None
    for prefix in _PREFIXES:
        if phone.startswith(prefix):
            return ZONE_GUESSES[prefix]
    return None


def match_zone(text: str) -> str | None:
    """The IANA zone ``text`` names, or ``None`` if it names none or more than one."""
    if len(text) > _MAX_INPUT:
        return None
    key = _key(text)
    if not key:
        return None
    aliases, full, last = _index()
    return aliases.get(key) or full.get(key) or last.get(key)


__all__ = ["ZONE_GUESSES", "guess_zone", "match_zone"]
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_timezones.py -q`
Expected: all pass. A failing match case means the zone database differs from tzdata 2026.4. Check it with `uv run python -c "import zoneinfo; print(sorted(z for z in zoneinfo.available_timezones() if 'Cordoba' in z))"` before touching the code.

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: clean.

```bash
git add pyproject.toml uv.lock src/personal_organizer/messaging/timezones.py tests/unit/test_timezones.py
git commit -m "feat: time zone guess from the country code, and city matching"
```

---

### Task 3: The two-language onboarding text table (D11)

**Files:**
- Create: `src/personal_organizer/messaging/onboarding_text.py`
- Test: `tests/unit/test_onboarding_text.py`

**Interfaces:**
- Consumes: `detect_language` (Task 1, tests only), `LANGUAGES` from `personal_organizer.db.models` (PR 2, tests only).
- Produces:
  - `ONBOARDING_TEXT: Final[Mapping[str, Mapping[str, str]]]`, keyed language → key → template.
  - `t(key: str, language: str, **values: str) -> str`. Unknown language → English. Unknown key or a missing value → `KeyError`.
  - Keys: `welcome`, `zone_confirm` (`zone`), `option_correct`, `option_change`, `zone_ask_city`, `zone_retry`, `zone_retry_keep` (`zone`), `zone_set` (`zone`), `connect_link` (`url`), `all_set`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_onboarding_text.py`:

```python
from __future__ import annotations

from string import Formatter

import pytest

from personal_organizer.db.models import LANGUAGES
from personal_organizer.messaging.language import detect_language
from personal_organizer.messaging.onboarding_text import ONBOARDING_TEXT, t

URL = "https://po.example.test/connect/abc.def"


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


class TestTable:
    def test_exactly_the_tenant_languages(self) -> None:
        assert set(ONBOARDING_TEXT) == set(LANGUAGES)

    def test_both_languages_have_the_same_keys(self) -> None:
        assert set(ONBOARDING_TEXT["he"]) == set(ONBOARDING_TEXT["en"])

    def test_both_languages_take_the_same_values(self) -> None:
        for key, english in ONBOARDING_TEXT["en"].items():
            assert _fields(ONBOARDING_TEXT["he"][key]) == _fields(english), key

    def test_the_keys_other_prs_use_exist(self) -> None:
        for language in LANGUAGES:
            assert URL in t("connect_link", language, url=URL)
            assert t("all_set", language)

    def test_every_hebrew_string_is_hebrew(self) -> None:
        for key, template in ONBOARDING_TEXT["he"].items():
            assert detect_language(template) == "he", key


class TestT:
    def test_formats_values(self) -> None:
        assert t("zone_set", "en", zone="Europe/London") == (
            "Your time zone is set to Europe/London."
        )

    def test_the_link_is_on_its_own_line(self) -> None:
        # WhatsApp linkifies a URL reliably only when nothing is glued to it.
        for language in LANGUAGES:
            assert t("connect_link", language, url=URL).endswith(f"\n{URL}")

    def test_an_unknown_language_falls_back_to_english(self) -> None:
        assert t("all_set", "fr") == t("all_set", "en")

    def test_an_unknown_key_raises(self) -> None:
        with pytest.raises(KeyError):
            t("no_such_key", "en")

    def test_a_missing_value_raises(self) -> None:
        with pytest.raises(KeyError):
            t("connect_link", "en")
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_onboarding_text.py -q`
Expected: `ModuleNotFoundError: No module named 'personal_organizer.messaging.onboarding_text'`.

- [ ] **Step 3: Implement**

`src/personal_organizer/messaging/onboarding_text.py`:

```python
"""Every line onboarding sends, in both languages (D11).

One table, so a missing translation is a failing test (``test_onboarding_text.py`` checks
that the key sets and the ``{placeholders}`` match) rather than an English line in a Hebrew
conversation. The templates hold no braces besides their placeholders, and values are user-
or system-supplied strings (a zone name, a URL) inserted with ``format_map``. A value is
never itself parsed as a template.

``connect_link`` ends with the URL on a line of its own. WhatsApp linkifies a bare URL
reliably, but not one with a character glued to it. ``all_set`` is sent by the
``onboarding:connected`` task (PR 4). ``zone_retry_keep`` says "reply 1", which relies on
Correct being the first option of ``messaging.onboarding.zone_choice``. A test there pins
that order.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import ENGLISH

_EN: Final = {
    "welcome": "Hi! I'm your personal organizer. Two quick steps and you're set up.",
    "zone_confirm": "Is your time zone {zone}?",
    "option_correct": "Correct",
    "option_change": "Change",
    "zone_ask_city": "Which city are you in? For example: London, or Europe/London.",
    "zone_retry": (
        "I couldn't match that to a time zone. Send a city, for example London, "
        "or a zone such as Europe/London."
    ),
    "zone_retry_keep": "Or reply 1 to keep {zone}.",
    "zone_set": "Your time zone is set to {zone}.",
    "connect_link": (
        "Now connect your Google Calendar. Open this link and follow the steps. "
        "It works once and expires soon:\n{url}"
    ),
    "all_set": "You're all set! Your Google Calendar is connected.",
}

_HE: Final = {
    "welcome": "היי! אני העוזר האישי שלך. עוד שני צעדים קצרים וסיימנו.",
    "zone_confirm": "האם אזור הזמן שלך הוא {zone}?",
    "option_correct": "נכון",
    "option_change": "לשנות",
    "zone_ask_city": "באיזו עיר את/ה? לדוגמה: תל אביב, או Europe/London.",
    "zone_retry": (
        "לא הצלחתי להתאים את זה לאזור זמן. אפשר לשלוח שם של עיר, לדוגמה תל אביב, "
        "או אזור כמו Europe/London."
    ),
    "zone_retry_keep": "או להשיב 1 כדי להשאיר את {zone}.",
    "zone_set": "אזור הזמן שלך נקבע ל-{zone}.",
    "connect_link": (
        "עכשיו נחבר את יומן Google שלך. יש לפתוח את הקישור ולעקוב אחרי השלבים. "
        "הוא עובד פעם אחת ותוקפו קצר:\n{url}"
    ),
    "all_set": "הכול מוכן! יומן Google שלך מחובר.",
}

ONBOARDING_TEXT: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"en": MappingProxyType(_EN), "he": MappingProxyType(_HE)}
)


def t(key: str, language: str, **values: str) -> str:
    """The ``key`` line in ``language`` (English if unknown), with ``values`` filled in."""
    table = ONBOARDING_TEXT.get(language, ONBOARDING_TEXT[ENGLISH])
    return table[key].format_map(values)


__all__ = ["ONBOARDING_TEXT", "t"]
```

- [ ] **Step 4: Run, lint, commit**

Run: `uv run pytest tests/unit/test_onboarding_text.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass, clean.

```bash
git add src/personal_organizer/messaging/onboarding_text.py tests/unit/test_onboarding_text.py
git commit -m "feat: onboarding text in English and Hebrew"
```

---

### Task 4: Signed link and state tokens

**Files:**
- Create: `src/personal_organizer/onboarding/__init__.py`
- Create: `src/personal_organizer/onboarding/tokens.py`
- Test: `tests/unit/test_onboarding_tokens.py`

**Interfaces:**
- Consumes: `itsdangerous` (already a dependency).
- Produces (PR 4 uses these for `/connect/{token}` and the callback `state`):
  - `LINK_SALT: Final = "po.onboarding.link"`, `STATE_SALT: Final = "po.onboarding.state"`
  - `@dataclass(frozen=True, slots=True) class TokenPayload: tenant_id: UUID; nonce: str`
  - `sign(payload: TokenPayload, *, secret: str, salt: str) -> str`
  - `verify(token: str, *, secret: str, salt: str, max_age_s: int) -> TokenPayload | None`, which returns `None` on a bad signature, expiry, a wrong salt or a bad shape.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_onboarding_tokens.py`:

```python
from __future__ import annotations

from uuid import uuid4

import pytest
from itsdangerous import URLSafeTimedSerializer

from personal_organizer.onboarding.tokens import (
    LINK_SALT,
    STATE_SALT,
    TokenPayload,
    sign,
    verify,
)

SECRET = "s" * 40
PAYLOAD = TokenPayload(tenant_id=uuid4(), nonce="n0nce-value")


def test_round_trip() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) == PAYLOAD


def test_url_safe() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert all(char.isalnum() or char in "-_." for char in token)


def test_a_link_token_is_not_a_state_token() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret=SECRET, salt=STATE_SALT, max_age_s=900) is None


def test_another_secret_fails() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret="t" * 40, salt=LINK_SALT, max_age_s=900) is None


def test_expired() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=-1) is None


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "eyJ0IjoiYSJ9.AAAA.BBBB"])
def test_garbage(token: str) -> None:
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


def test_tampered() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify(flipped, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


@pytest.mark.parametrize(
    "data",
    [
        ["not", "a", "dict"],
        {"t": "not-a-uuid", "n": "x"},
        {"t": str(uuid4())},
        {"t": str(uuid4()), "n": ""},
        {"t": str(uuid4()), "n": 7},
    ],
)
def test_a_validly_signed_bad_shape(data: object) -> None:
    token = URLSafeTimedSerializer(SECRET, salt=LINK_SALT).dumps(data)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_onboarding_tokens.py -q`
Expected: `ModuleNotFoundError: No module named 'personal_organizer.onboarding'`.

- [ ] **Step 3: Implement**

`src/personal_organizer/onboarding/__init__.py`:

```python
"""The web side of onboarding: signed tokens and the single-use connect links built on them.

The chat side (the steps, their text) lives in ``personal_organizer.messaging``. This package
is what both the worker, which sends a link, and the api, which serves ``/connect/{token}``
and Composio's callback, need to agree on.
"""
```

`src/personal_organizer/onboarding/tokens.py`:

```python
"""Signed, timestamped tokens for the connect link and the OAuth ``state`` (D4, D5).

``itsdangerous.URLSafeTimedSerializer`` signs ``{"t": tenant_id, "n": nonce}`` with HMAC under
``ONBOARDING__LINK_SECRET``. The **salt separates the two uses**: a link token is never
accepted as callback state, or the other way round, even though one secret signs both.

The signature proves that we issued the token and when. It does not make the token
single-use. That is the ``onboarding_links`` row keyed on ``nonce`` (``consume_link``). The
row's ``expires_at`` is the authority on expiry. ``max_age_s`` here is a second, cheaper
bound, checked before the database is touched.

The payload is signed, not encrypted: anyone holding the link can read the tenant id. It is
a random UUID that identifies nobody outside our database, and the link is a bearer
credential anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from itsdangerous import BadData, URLSafeTimedSerializer

LINK_SALT: Final = "po.onboarding.link"
STATE_SALT: Final = "po.onboarding.state"


@dataclass(frozen=True, slots=True)
class TokenPayload:
    tenant_id: UUID
    nonce: str


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def sign(payload: TokenPayload, *, secret: str, salt: str) -> str:
    return _serializer(secret, salt).dumps({"t": str(payload.tenant_id), "n": payload.nonce})


def verify(token: str, *, secret: str, salt: str, max_age_s: int) -> TokenPayload | None:
    """The payload of a token we signed under ``salt`` within ``max_age_s``, else ``None``."""
    try:
        data = _serializer(secret, salt).loads(token, max_age=max_age_s)
    except BadData:  # bad signature, expired, undecodable: all the same to the caller
        return None
    if not isinstance(data, dict):
        return None
    tenant, nonce = data.get("t"), data.get("n")
    if not isinstance(tenant, str) or not isinstance(nonce, str) or not nonce:
        return None
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        return None
    return TokenPayload(tenant_id=tenant_id, nonce=nonce)


__all__ = ["LINK_SALT", "STATE_SALT", "TokenPayload", "sign", "verify"]
```

- [ ] **Step 4: Run, lint, commit**

Run: `uv run pytest tests/unit/test_onboarding_tokens.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass, clean.

```bash
git add src/personal_organizer/onboarding tests/unit/test_onboarding_tokens.py
git commit -m "feat: signed onboarding link and state tokens"
```

---

### Task 5: `send_once` with an idempotency key (D6)

**Files:**
- Modify: `src/personal_organizer/messaging/outbox.py` (`_claim`, `send_once`, module docstring)
- Modify: `docs/adr/0003-replies-are-at-most-once-on-ambiguity.md` (Consequences)
- Create: `tests/fixtures/channels.py`
- Test: `tests/db/test_outbox.py`

**Interfaces:**
- Consumes: `ChannelOutbox.idempotency_key` (PR 2, `UNIQUE`).
- Produces:
  - `send_once(db, channel, *, inbox_id: UUID | None, kind: str, recipient_key: str, to: str, text: str, idempotency_key: str | None = None) -> str` claims on `(inbox_id, kind)` or on `idempotency_key`, and raises `ValueError` unless exactly one is set.
  - The test fixtures `FakeOutbound(*failures, mark_read_fails=False, name="whatsapp")` with `.attempts`, `.sent` and `.read`; `Spy()` with `.calls`; and `insert_inbox(conn, *, phone=SENDER_PHONE, user_id=None, body="hi", reply_id=None, message_type="text", channel="gowa", sent_at=None) -> UUID`.

- [ ] **Step 1: Create the shared test fakes**

`tests/fixtures/channels.py` (`FakeOutbound` and `Spy` are copies of the ones in `tests/db/test_handle_inbound.py`, which stays untouched):

```python
"""Fakes for the outbound half of a channel, and a raw inbox insert, for the worker-side DB tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from personal_organizer.core.errors import ChannelError, TransientChannelError
from personal_organizer.interfaces.channel import OutboundChannel, OutboundMessage
from personal_organizer.messaging.inbound import InboxRow
from tests.fixtures.payloads import SENDER_PHONE


class FakeOutbound:
    name = "whatsapp"

    def __init__(
        self, *failures: ChannelError, mark_read_fails: bool = False, name: str = "whatsapp"
    ) -> None:
        self.name = name
        self.failures = list(failures)
        self.mark_read_fails = mark_read_fails
        self.attempts: list[OutboundMessage] = []
        self.sent: list[OutboundMessage] = []
        self.read: list[str] = []

    async def send_text(self, message: OutboundMessage) -> str:
        self.attempts.append(message)
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)
        return f"{self.name}.OUT{len(self.sent)}"

    async def mark_read(self, provider_message_id: str) -> None:
        if self.mark_read_fails:
            raise TransientChannelError("provider down")
        self.read.append(provider_message_id)


class Spy:
    """Stands in for the agent handoff. Anything reaching it would, from Iteration 04,
    cost an LLM call."""

    def __init__(self) -> None:
        self.calls: list[InboxRow] = []

    async def __call__(self, row: InboxRow, channel: OutboundChannel) -> None:
        self.calls.append(row)


async def insert_inbox(
    conn: Any,
    *,
    phone: str | None = SENDER_PHONE,
    user_id: str | None = None,
    body: str | None = "hi",
    reply_id: str | None = None,
    message_type: str = "text",
    channel: str = "gowa",
    sent_at: datetime | None = None,
) -> UUID:
    """A row as ingress would store it. The sender key follows ``SenderRef.key``: BSUID first."""
    key = f"uid:{user_id}" if user_id else f"tel:{phone}"
    raw = json.dumps({"body": body}) if body is not None else None
    inbox_id: UUID = await conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_user_id, "
        "sender_phone, message_type, body, reply_id, raw, sent_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10) RETURNING id",
        channel,
        f"wamid.{uuid4().hex}",
        key,
        user_id,
        phone,
        message_type,
        body,
        reply_id,
        raw,
        sent_at or datetime.now(UTC),
    )
    return inbox_id
```

`InboxRow` comes from `messaging.inbound` for now. Task 7 moves it to `messaging.inbox`, and its Step 6 switches this import.

- [ ] **Step 2: Write the failing tests**

`tests/db/test_outbox.py`:

```python
"""``send_once`` claimed on an idempotency key (D6): every ADR 0003 path, for a send that
answers no inbound message. The ``(inbox_id, kind)`` paths are pinned in
``test_handle_inbound.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.messaging.outbox import send_once
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, insert_inbox
from tests.fixtures.payloads import SENDER_PHONE

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables")]

KEY = "connected:0b7c2c84"
KIND = "onboarding:all_set"
TEXT = "You're all set! Your Google Calendar is connected."
RECIPIENT = f"tel:{SENDER_PHONE}"


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _send(
    db: Database, channel: FakeOutbound, *, key: str = KEY, kind: str = KIND
) -> str:
    return await send_once(
        db,
        channel,
        inbox_id=None,
        kind=kind,
        recipient_key=RECIPIENT,
        to=SENDER_PHONE,
        text=TEXT,
        idempotency_key=key,
    )


async def _outbox(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT inbox_id, kind, idempotency_key, status, error_code FROM channel_outbox "
        "ORDER BY created_at"
    )
    return [dict(row) for row in rows]


class TestIdempotencyKey:
    async def test_sends_once_however_often_it_is_called(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound()
        assert [await _send(db, channel) for _ in range(3)] == ["accepted"] * 3
        assert channel.sent == [OutboundMessage(recipient=SENDER_PHONE, body=TEXT)]
        assert await _outbox(owner_conn) == [
            {"inbox_id": None, "kind": KIND, "idempotency_key": KEY, "status": "accepted",
             "error_code": None}
        ]

    async def test_distinct_keys_are_distinct_sends(self, db: Database) -> None:
        channel = FakeOutbound()
        await _send(db, channel, key="connected:a")
        await _send(db, channel, key="connected:b")
        assert len(channel.sent) == 2

    async def test_the_key_is_global_across_kinds(self, db: Database) -> None:
        """A key names one event. Reusing it under another kind is the same send."""
        channel = FakeOutbound()
        await _send(db, channel, kind="onboarding:all_set")
        await _send(db, channel, kind="reminder")
        assert len(channel.sent) == 1

    async def test_transient_then_success_sends_once(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound(TransientChannelError("429"))
        with pytest.raises(TransientChannelError):
            await _send(db, channel)
        assert (await _outbox(owner_conn))[0]["status"] == "pending"

        assert await _send(db, channel) == "accepted"
        assert len(channel.attempts) == 2
        assert len(channel.sent) == 1

    async def test_an_ambiguous_send_is_never_retried(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound(AmbiguousDeliveryError("read timeout"))
        assert await _send(db, channel) == "unknown"
        assert await _send(db, channel) == "unknown"
        assert len(channel.attempts) == 1

    async def test_a_send_interrupted_mid_flight_is_marked_unknown_not_resent(
        self, db: Database, owner_conn: Any
    ) -> None:
        await owner_conn.execute(
            "INSERT INTO channel_outbox (channel, kind, recipient_key, idempotency_key, status) "
            "VALUES ('whatsapp', $1, $2, $3, 'sending')",
            KIND,
            RECIPIENT,
            KEY,
        )
        channel = FakeOutbound()
        assert await _send(db, channel) == "unknown"
        assert channel.attempts == []

    async def test_a_rejected_send_is_recorded_and_not_retried(
        self, db: Database, owner_conn: Any
    ) -> None:
        channel = FakeOutbound(RejectedChannelError(131047))
        assert await _send(db, channel) == "failed"
        assert await _send(db, channel) == "failed"
        assert len(channel.attempts) == 1
        assert (await _outbox(owner_conn))[0]["error_code"] == 131047

    async def test_keyed_and_inbox_sends_do_not_collide(
        self, db: Database, owner_conn: Any
    ) -> None:
        inbox_id: UUID = await insert_inbox(owner_conn)
        channel = FakeOutbound()
        await send_once(
            db, channel, inbox_id=inbox_id, kind=KIND, recipient_key=RECIPIENT,
            to=SENDER_PHONE, text=TEXT,
        )
        await _send(db, channel)
        assert len(channel.sent) == 2
        assert {row["idempotency_key"] for row in await _outbox(owner_conn)} == {None, KEY}

    async def test_the_outbox_holds_no_text(self, db: Database, owner_conn: Any) -> None:
        await _send(db, FakeOutbound())
        rows = await owner_conn.fetch("SELECT row_to_json(o)::text AS j FROM channel_outbox o")
        assert rows
        assert all("all set" not in row["j"] for row in rows)


class TestArguments:
    @pytest.mark.parametrize("both", [True, False])
    async def test_exactly_one_of_inbox_id_and_key(
        self, db: Database, owner_conn: Any, both: bool
    ) -> None:
        inbox_id = await insert_inbox(owner_conn) if both else None
        with pytest.raises(ValueError, match="exactly one"):
            await send_once(
                db, FakeOutbound(), inbox_id=inbox_id, kind=KIND, recipient_key=RECIPIENT,
                to=SENDER_PHONE, text=TEXT, idempotency_key=KEY if both else None,
            )
        assert await _outbox(owner_conn) == []
```

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/db/test_outbox.py -q`
Expected: failures. The send calls fail with `TypeError: send_once() got an unexpected keyword argument 'idempotency_key'`.

- [ ] **Step 4: Implement**

In `src/personal_organizer/messaging/outbox.py`:

1. Change the first paragraph of the module docstring:

```python
"""Sending a reply at most once.

WhatsApp's Cloud API has no idempotency key, and a job can run more than once: Procrastinate
retries a transient failure, and a job interrupted by a deploy is retried from the top. So
each send is claimed in ``channel_outbox`` before it is sent. A reply to an inbound message
is keyed on ``(inbox_id, kind)``. A send that answers nothing ("You're all set", reminders
later) is keyed on an ``idempotency_key`` that names the event, e.g.
``connected:<connection_id>`` (D6). The key is unique across kinds and must never contain
message content. Either way the row moves through::
```

Keep the rest of the docstring (the state diagram and the ADR paragraph) as it is.

2. Add `and_` to the sqlalchemy import: `from sqlalchemy import and_, func, select, update`.

3. Replace `_claim` and `send_once`'s signature and claim call:

```python
async def _claim(
    db: Database,
    *,
    channel: str,
    inbox_id: UUID | None,
    kind: str,
    recipient_key: str,
    idempotency_key: str | None,
) -> tuple[UUID, str]:
    """Create or lock this send's row and move it to ``sending`` if it is ours to send.

    Returns the row id and the status the caller should act on: ``sending`` means send now;
    anything in :data:`SETTLED` means a previous attempt already decided the outcome.
    """
    if idempotency_key is not None:
        conflict = ["idempotency_key"]
        this_send = ChannelOutbox.idempotency_key == idempotency_key
    else:
        conflict = ["inbox_id", "kind"]
        this_send = and_(ChannelOutbox.inbox_id == inbox_id, ChannelOutbox.kind == kind)
    async with db.system_session() as session:
        await session.execute(
            insert(ChannelOutbox)
            .values(
                channel=channel,
                inbox_id=inbox_id,
                kind=kind,
                recipient_key=recipient_key,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=conflict)
        )
        row = (
            await session.execute(select(ChannelOutbox).where(this_send).with_for_update())
        ).scalar_one()
        if row.status == "sending":
            # The previous attempt died between claiming and recording the outcome.
            row.status, row.updated_at = "unknown", datetime.now(UTC)
            log.warning("outbox.outcome_unknown", outbox_id=str(row.id), reason="interrupted")
        elif row.status == "pending":
            row.status, row.updated_at = "sending", datetime.now(UTC)
            return row.id, "sending"
        return row.id, row.status


async def send_once(
    db: Database,
    channel: OutboundChannel,
    *,
    inbox_id: UUID | None,
    kind: str,
    recipient_key: str,
    to: str,
    text: str,
    idempotency_key: str | None = None,
) -> str:
    """Send ``text`` to ``to`` at most once: as the ``kind`` reply to ``inbox_id``, or as the
    send named by ``idempotency_key``. Exactly one of the two is given.

    Returns the send's resulting status. Re-raises :class:`TransientChannelError` -- and only
    that -- after putting the row back to ``pending``, so the job's retry sends it again.
    """
    if (inbox_id is None) == (idempotency_key is None):
        msg = "send_once needs exactly one of inbox_id and idempotency_key"
        raise ValueError(msg)
    outbox_id, status = await _claim(
        db,
        channel=channel.name,
        inbox_id=inbox_id,
        kind=kind,
        recipient_key=recipient_key,
        idempotency_key=idempotency_key,
    )
```

Leave the rest of `send_once` (the try/except and the `accepted` path) unchanged.

- [ ] **Step 5: Run the new and the old tests**

Run: `uv run pytest tests/db/test_outbox.py tests/db/test_handle_inbound.py -q`
Expected: all pass. `test_handle_inbound.py` exercises the unchanged `(inbox_id, kind)` path.

- [ ] **Step 6: Record it in ADR 0003**

Append to the `## Consequences` list of `docs/adr/0003-replies-are-at-most-once-on-ambiguity.md`:

```markdown
- From Iteration 03 a send that answers no inbound message claims on
  `channel_outbox.idempotency_key` instead (D6). The first is "You're all set" under
  `connected:<connection_id>`, and reminders will follow. The states and the rule are the same.
  The key names the event, is unique across kinds and never carries content.
  `tests/db/test_outbox.py` pins the same paths for it.
```

- [ ] **Step 7: Lint, type-check, commit**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: clean.

```bash
git add src/personal_organizer/messaging/outbox.py tests/fixtures/channels.py tests/db/test_outbox.py \
        docs/adr/0003-replies-are-at-most-once-on-ambiguity.md
git commit -m "feat: send_once claims on an idempotency key for sends that answer nothing"
```

---

### Task 6: Single-use connect links, issued or reused

**Files:**
- Create: `src/personal_organizer/onboarding/links.py`
- Create: `tests/fixtures/tenants.py`
- Modify: `tests/db/conftest.py` (add `clean_tenant_tables`, `onboarding_settings`)
- Test: `tests/db/test_onboarding_links.py`

**Interfaces:**
- Consumes: `sign`, `TokenPayload`, `LINK_SALT` (Task 4). From PR 2: `create_link`, `latest_usable_link` and `consume_link` (`db/repositories/links.py`), `create_tenant` (`db/repositories/tenants.py`), `NETWORK_WHATSAPP` and `OnboardingLink` (`db/models`).
- Produces:
  - `issue_or_reuse_link(db: Database, tenant_id: UUID, *, settings: Settings, now: datetime) -> str` returns `f"{settings.app.public_base_url}/connect/{token}"`. It reuses the newest unused link only while at least half of `ONBOARDING__LINK_TTL_S` remains. Each call re-signs a token for the same nonce, so the token's signature time is the time of issue and the row's `expires_at` is the authority. It raises `RuntimeError` when the public URL or the link secret is unset.
  - Test fixtures `clean_tenant_tables` and `onboarding_settings` (Composio on). `tests/fixtures/tenants.py` holds `BASE_URL`, `LINK_SECRET`, `IL_PHONE`, `US_PHONE`, `with_onboarding(settings)`, `new_tenant(db, ...)`, `tenant_by_phone(db, phone)`, `messages_of(db, tenant_id)`, `links_of(db, tenant_id)` and `link_payload(text)`.

- [ ] **Step 1: Add the shared tenant fixtures**

`tests/fixtures/tenants.py`:

```python
"""Settings, numbers and helpers for the onboarding tests.

Tenant tables are under FORCE RLS, so even the owner sees nothing without the GUC. These
helpers read through ``Database.tenant_session``, the way the application does, and find a
tenant through the ``resolve_tenant`` definer function.
"""

from __future__ import annotations

import re
from typing import Final
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, update

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models import NETWORK_WHATSAPP, Message, OnboardingLink, Tenant
from personal_organizer.db.repositories.tenants import (
    activate,
    create_tenant,
    get_tenant,
    resolve_tenant,
    set_onboarding_step,
)
from personal_organizer.onboarding.tokens import LINK_SALT, TokenPayload, verify
from personal_organizer.settings import ComposioSettings, OnboardingSettings, Settings
from tests.fixtures.payloads import SENDER_PHONE

BASE_URL: Final = "https://po.example.test"
LINK_SECRET: Final = "test-link-secret-" + "x" * 32
IL_PHONE: Final = "+972501234567"
US_PHONE: Final = "+12025550123"
#: ``SENDER_PHONE`` is Dutch: its guess is Europe/Amsterdam.
NL_PHONE: Final = SENDER_PHONE

_LINK: Final = re.compile(re.escape(BASE_URL) + r"/connect/(\S+)")


def with_onboarding(settings: Settings) -> Settings:
    """``settings`` with Composio on, plus everything its validator would demand."""
    return settings.model_copy(
        update={
            "app": settings.app.model_copy(update={"public_base_url": BASE_URL}),
            "composio": ComposioSettings(
                enabled=True,
                api_key=SecretStr("test-composio-key"),
                calendar_auth_config_id="ac_test",
            ),
            "onboarding": OnboardingSettings(link_secret=SecretStr(LINK_SECRET)),
        }
    )


async def new_tenant(
    db: Database,
    *,
    phone: str | None = NL_PHONE,
    user_id: str | None = None,
    language: str = "en",
    status: str = "onboarding",
    step: str | None = "zone",
) -> UUID:
    key = f"uid:{user_id}" if user_id else f"tel:{phone}"
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session, network=NETWORK_WHATSAPP, external_id=key, phone=phone, language=language
        )
    async with db.tenant_session(TenantId(tenant_id)) as session:
        if status == "active":
            await activate(session, tenant_id)
        elif status != "onboarding":
            await session.execute(
                update(Tenant).where(Tenant.id == tenant_id).values(status=status)
            )
        if status != "active" and step != "zone":
            await set_onboarding_step(session, tenant_id, step)
    return tenant_id


async def tenant_by_phone(db: Database, phone: str) -> Tenant | None:
    async with db.system_session() as session:
        tenant_id = await resolve_tenant(
            session, network=NETWORK_WHATSAPP, external_id=f"tel:{phone}"
        )
    if tenant_id is None:
        return None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await get_tenant(session, tenant_id)


async def messages_of(db: Database, tenant_id: UUID) -> list[Message]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return list(await session.scalars(select(Message).order_by(Message.created_at)))


async def links_of(db: Database, tenant_id: UUID) -> list[OnboardingLink]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return list(
            await session.scalars(select(OnboardingLink).order_by(OnboardingLink.created_at))
        )


def link_payload(text: str) -> TokenPayload:
    """The verified payload of the one connect link in ``text``. Fails the test if there is none."""
    found = _LINK.search(text)
    assert found is not None, "no connect link in the message"
    payload = verify(found.group(1), secret=LINK_SECRET, salt=LINK_SALT, max_age_s=3600)
    assert payload is not None, "the link does not verify"
    return payload
```

- [ ] **Step 2: Add the conftest fixtures**

Append to `tests/db/conftest.py`. If PR 2 already defines a `clean_tenant_tables` fixture, keep PR 2's and add only `onboarding_settings`.

```python
@pytest.fixture
async def clean_tenant_tables(owner_conn: Any) -> AsyncIterator[None]:
    """Empty every tenant table before and after a test. Each one references ``tenants``
    with ``ON DELETE CASCADE``, so truncating it cascades. TRUNCATE is not subject to RLS."""
    await owner_conn.execute("TRUNCATE tenants CASCADE")
    try:
        yield
    finally:
        await owner_conn.execute("TRUNCATE tenants CASCADE")


@pytest.fixture
def onboarding_settings(db_settings: Settings) -> Settings:
    """The real database settings with Composio, and so onboarding, switched on."""
    return with_onboarding(db_settings)
```

and add `from tests.fixtures.tenants import with_onboarding` to its imports.

- [ ] **Step 3: Write the failing tests**

`tests/db/test_onboarding_links.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.onboarding.tokens import TokenPayload
from personal_organizer.settings import Settings
from tests.fixtures.tenants import BASE_URL, IL_PHONE, link_payload, links_of, new_tenant

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def test_issues_a_signed_single_use_link(
    db: Database, onboarding_settings: Settings
) -> None:
    tenant_id = await new_tenant(db)
    url = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)

    assert url.startswith(f"{BASE_URL}/connect/")
    [link] = await links_of(db, tenant_id)
    assert link_payload(url) == TokenPayload(tenant_id=tenant_id, nonce=link.nonce)
    ttl = timedelta(seconds=onboarding_settings.onboarding.link_ttl_s)
    assert link.expires_at == NOW + ttl
    assert link.used_at is None


async def test_reuses_the_latest_usable_link(db: Database, onboarding_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    first = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    second = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    assert link_payload(first).nonce == link_payload(second).nonce
    assert len(await links_of(db, tenant_id)) == 1


async def test_a_used_link_is_replaced(db: Database, onboarding_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    first = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        assert await consume_link(session, tenant_id, nonce=link_payload(first).nonce, now=NOW)

    second = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    assert link_payload(second).nonce != link_payload(first).nonce
    assert len(await links_of(db, tenant_id)) == 2


@pytest.mark.parametrize(("elapsed", "reused"), [(0.4, True), (0.6, False), (1.5, False)])
async def test_a_link_past_half_its_life_is_replaced(
    db: Database, onboarding_settings: Settings, elapsed: float, reused: bool
) -> None:
    """Review Focus 2: a reused link must leave the user time to switch apps and sign in."""
    tenant_id = await new_tenant(db)
    ttl = onboarding_settings.onboarding.link_ttl_s
    first = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    later = NOW + timedelta(seconds=ttl * elapsed)
    second = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=later)
    assert (link_payload(first).nonce == link_payload(second).nonce) is reused


async def test_links_are_per_tenant(db: Database, onboarding_settings: Settings) -> None:
    mine, theirs = await new_tenant(db), await new_tenant(db, phone=IL_PHONE)
    await issue_or_reuse_link(db, mine, settings=onboarding_settings, now=NOW)
    url = await issue_or_reuse_link(db, theirs, settings=onboarding_settings, now=NOW)
    assert link_payload(url).tenant_id == theirs
    assert len(await links_of(db, theirs)) == 1


async def test_needs_the_public_url_and_the_secret(db: Database, db_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    bare = db_settings.model_copy(
        update={"app": db_settings.app.model_copy(update={"public_base_url": None})}
    )
    with pytest.raises(RuntimeError, match="APP__PUBLIC_BASE_URL"):
        await issue_or_reuse_link(db, tenant_id, settings=bare, now=NOW)
```

- [ ] **Step 4: Run and watch them fail**

Run: `uv run pytest tests/db/test_onboarding_links.py -q`
Expected: `ModuleNotFoundError: No module named 'personal_organizer.onboarding.links'`.

- [ ] **Step 5: Implement**

`src/personal_organizer/onboarding/links.py`:

```python
"""The connect link a tenant is sent in the ``connect`` step (D4, D10).

A link is a signed token (:mod:`.tokens`) over the tenant id and a random nonce, plus an
``onboarding_links`` row that makes it single-use and bounds it to ``ONBOARDING__LINK_TTL_S``.
Every message in the ``connect`` step re-sends a link, so links are **reused**: the newest
unused one is sent again, as long as at least half its life remains. A link with seconds
left would expire while the user switches to the browser and signs in to Google. Only then
is a fresh one issued. A user who writes five times holds one live link, not five.

The token is re-signed on every call, so its signature timestamp is the time it was sent,
not the time the row was created. The row's ``expires_at`` stays the authority, and
``consume_link`` checks it.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.links import create_link, latest_usable_link
from personal_organizer.onboarding.tokens import LINK_SALT, TokenPayload, sign
from personal_organizer.settings import Settings

#: 128 bits: unguessable, and short enough to keep the URL tidy in a chat bubble.
NONCE_BYTES: Final = 16


async def issue_or_reuse_link(
    db: Database, tenant_id: UUID, *, settings: Settings, now: datetime
) -> str:
    """The full URL of a usable connect link for ``tenant_id``, issuing one if needed."""
    base_url = settings.app.public_base_url
    secret = settings.onboarding.link_secret
    if base_url is None or secret is None:
        msg = "connect links need APP__PUBLIC_BASE_URL and ONBOARDING__LINK_SECRET"
        raise RuntimeError(msg)
    ttl = timedelta(seconds=settings.onboarding.link_ttl_s)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        # "Usable" here means "still usable at half-life from now".
        link = await latest_usable_link(session, tenant_id, now=now + ttl / 2)
        if link is not None:
            nonce = link.nonce
        else:
            nonce = secrets.token_urlsafe(NONCE_BYTES)
            await create_link(session, tenant_id, nonce=nonce, expires_at=now + ttl)
    token = sign(
        TokenPayload(tenant_id=tenant_id, nonce=nonce),
        secret=secret.get_secret_value(),
        salt=LINK_SALT,
    )
    return f"{base_url}/connect/{token}"


__all__ = ["NONCE_BYTES", "issue_or_reuse_link"]
```

- [ ] **Step 6: Run and watch them pass**

Run: `uv run pytest tests/db/test_onboarding_links.py -q`
Expected: all pass.

- [ ] **Step 7: Lint, type-check, commit**

Run: `uv run pytest tests/db/test_onboarding_links.py -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: pass, clean.

```bash
git add src/personal_organizer/onboarding/links.py tests/fixtures/tenants.py tests/db/conftest.py \
        tests/db/test_onboarding_links.py
git commit -m "feat: issue or reuse a single-use connect link"
```

---

### Task 7: The inbox snapshot and tenant identities (D12)

**Files:**
- Create: `src/personal_organizer/messaging/inbox.py` (`InboxRow` moved here, plus `sender_user_id`, `reply_id`; `load_row`)
- Create: `src/personal_organizer/messaging/tenancy.py`
- Modify: `src/personal_organizer/messaging/inbound.py` (import `InboxRow`/`load_row` from `inbox`, delete its own `InboxRow`/`_load`, keep `InboxRow` in `__all__`)
- Modify: `src/personal_organizer/db/repositories/tenants.py` (PR 2's; add `add_identity`)
- Modify: `tests/fixtures/channels.py` (import `InboxRow` from `messaging.inbox`)
- Test: `tests/unit/test_tenancy.py`, `tests/db/test_tenancy.py`

**Interfaces:**
- Consumes: from PR 2, `resolve_tenant`, `create_tenant`, `get_tenant` and `primary_phone` (`db/repositories/tenants.py`), `latest_inbound_channel` (`db/repositories/messages.py`), and `NETWORK_WHATSAPP` and `TenantIdentity` (`db/models`).
- Produces:
  - `InboxRow` fields, in order: `id, channel, provider_message_id, sender_key, sender_user_id, sender_phone, message_type, body, reply_id, sent_at, processed_at, disposition`. Also `InboxRow.of(row: ChannelInbox)` and `load_row(db: Database, inbox_id: UUID) -> InboxRow | None`.
  - `add_identity(session, tenant_id: UUID, *, network: str, external_id: str, phone: str | None) -> None` (a no-op if the key exists).
  - `network_for(channel: str) -> str` returns `"whatsapp"` for `"whatsapp"` and `"gowa"`, and raises `ValueError` otherwise.
  - `identity_keys(row: InboxRow) -> tuple[str, ...]` returns `uid:` then `tel:`, whichever are present. `[0] == row.sender_key`.
  - `resolve_sender(db, row) -> UUID | None` tries each key and records any stronger keys that were missing.
  - `enrol(db, row, *, language: str) -> UUID` creates the tenant on the strongest key and adds the others.
  - `@dataclass(frozen=True, slots=True) class TenantState: id: UUID; status: str; step: str | None; language: str; first_message: bool`
  - `load_state(db, tenant_id: UUID) -> TenantState | None`. `first_message` is True when no inbound message of the tenant's is recorded yet.
  - `phone_of(db, tenant_id: UUID) -> str | None`, which wraps `primary_phone`.

- [ ] **Step 1: Write the failing unit tests**

`tests/unit/test_tenancy.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from personal_organizer.db.models import NETWORK_WHATSAPP
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.tenancy import identity_keys, network_for


def _row(*, user_id: str | None, phone: str | None, channel: str = "whatsapp") -> InboxRow:
    return InboxRow(
        id=uuid4(),
        channel=channel,
        provider_message_id="wamid.X",
        sender_key=f"uid:{user_id}" if user_id else f"tel:{phone}",
        sender_user_id=user_id,
        sender_phone=phone,
        message_type="text",
        body="hi",
        reply_id=None,
        sent_at=datetime.now(UTC),
        processed_at=None,
        disposition=None,
    )


@pytest.mark.parametrize("channel", ["whatsapp", "gowa"])
def test_both_whatsapp_channels_are_one_network(channel: str) -> None:
    assert network_for(channel) == NETWORK_WHATSAPP == "whatsapp"


def test_an_unknown_channel_has_no_network() -> None:
    with pytest.raises(ValueError, match="telegram"):
        network_for("telegram")


@pytest.mark.parametrize(
    ("user_id", "phone", "keys"),
    [
        ("US.1", "+31612345678", ("uid:US.1", "tel:+31612345678")),
        (None, "+31612345678", ("tel:+31612345678",)),
        ("US.1", None, ("uid:US.1",)),
    ],
)
def test_identity_keys_strongest_first(
    user_id: str | None, phone: str | None, keys: tuple[str, ...]
) -> None:
    row = _row(user_id=user_id, phone=phone)
    assert identity_keys(row) == keys
    assert identity_keys(row)[0] == row.sender_key
```

- [ ] **Step 2: Write the failing DB tests**

`tests/db/test_tenancy.py`:

```python
"""Resolving a sender to a tenant through network-keyed identities (D12)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models import NETWORK_WHATSAPP
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import resolve_tenant
from personal_organizer.messaging.inbox import InboxRow, load_row
from personal_organizer.messaging.tenancy import (
    TenantState,
    enrol,
    load_state,
    phone_of,
    resolve_sender,
)
from personal_organizer.settings import Settings
from tests.fixtures.channels import insert_inbox
from tests.fixtures.tenants import NL_PHONE

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables")]


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _row(
    db: Database, conn: Any, *, user_id: str | None = None, phone: str | None = NL_PHONE,
    channel: str = "gowa",
) -> InboxRow:
    row = await load_row(db, await insert_inbox(conn, user_id=user_id, phone=phone, channel=channel))
    assert row is not None
    return row


async def _resolve(db: Database, key: str) -> Any:
    async with db.system_session() as session:
        return await resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=key)


class TestIdentities:
    async def test_an_unknown_sender_resolves_to_nothing(self, db: Database, owner_conn: Any) -> None:
        assert await resolve_sender(db, await _row(db, owner_conn)) is None

    async def test_enrolled_by_phone_resolves_on_either_channel(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn, channel="gowa"), language="en")
        via_meta = await _row(db, owner_conn, channel="whatsapp")
        assert await resolve_sender(db, via_meta) == tenant_id

    async def test_enrolled_with_a_bsuid_and_a_phone_records_both(
        self, db: Database, owner_conn: Any
    ) -> None:
        row = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        tenant_id = await enrol(db, row, language="en")
        assert await _resolve(db, "uid:US.1") == tenant_id
        assert await _resolve(db, f"tel:{NL_PHONE}") == tenant_id

    async def test_a_bsuid_for_a_phone_keyed_tenant_is_added_not_a_second_tenant(
        self, db: Database, owner_conn: Any
    ) -> None:
        """Review Focus 3: first seen on the gateway by number, later on Meta with a BSUID."""
        tenant_id = await enrol(db, await _row(db, owner_conn), language="en")
        with_bsuid = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await resolve_sender(db, with_bsuid) == tenant_id
        assert await _resolve(db, "uid:US.1") == tenant_id
        hidden_number = await _row(db, owner_conn, user_id="US.1", phone=None, channel="whatsapp")
        assert await resolve_sender(db, hidden_number) == tenant_id

    async def test_enrol_is_idempotent(self, db: Database, owner_conn: Any) -> None:
        row = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await enrol(db, row, language="en") == await enrol(db, row, language="he")


class TestState:
    async def test_a_new_tenant_is_onboarding_its_first_message(
        self, db: Database, owner_conn: Any
    ) -> None:
        row = await _row(db, owner_conn)
        tenant_id = await enrol(db, row, language="he")
        assert await load_state(db, tenant_id) == TenantState(
            id=tenant_id, status="onboarding", step="zone", language="he", first_message=True
        )
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await record_inbound(
                session, tenant_id, inbox_id=row.id, channel=row.channel,
                message_type=row.message_type, body=row.body, sent_at=row.sent_at,
            )
        state = await load_state(db, tenant_id)
        assert state is not None
        assert state.first_message is False

    async def test_an_unknown_tenant_has_no_state(self, db: Database) -> None:
        assert await load_state(db, uuid4()) is None

    async def test_phone_of(self, db: Database, owner_conn: Any) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn), language="en")
        assert await phone_of(db, tenant_id) == NL_PHONE
```

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/unit/test_tenancy.py tests/db/test_tenancy.py -q`
Expected: `ModuleNotFoundError: No module named 'personal_organizer.messaging.inbox'`.

- [ ] **Step 4: Create `messaging/inbox.py`**

`src/personal_organizer/messaging/inbox.py`:

```python
"""The worker's snapshot of one ``channel_inbox`` row.

Read once at the start of the job and passed down, so every decision the job makes is made
from the same values. In its own module so the tenant lookup and the onboarding steps can
take it without importing the gate (``messaging.inbound``) that calls them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: UUID
    channel: str
    provider_message_id: str
    #: ``SenderRef.key``: ``uid:<BSUID>`` when the provider gave one, ``tel:<E.164>`` otherwise.
    sender_key: str
    sender_user_id: str | None
    sender_phone: str | None
    message_type: str
    body: str | None
    #: The id of the button, list row or GOWA selection the user picked, if any (D9).
    reply_id: str | None
    sent_at: datetime
    processed_at: datetime | None
    disposition: str | None

    @classmethod
    def of(cls, row: ChannelInbox) -> InboxRow:
        return cls(
            id=row.id,
            channel=row.channel,
            provider_message_id=row.provider_message_id,
            sender_key=row.sender_key,
            sender_user_id=row.sender_user_id,
            sender_phone=row.sender_phone,
            message_type=row.message_type,
            body=row.body,
            reply_id=row.reply_id,
            sent_at=row.sent_at,
            processed_at=row.processed_at,
            disposition=row.disposition,
        )


async def load_row(db: Database, inbox_id: UUID) -> InboxRow | None:
    async with db.system_session() as session:
        row = await session.get(ChannelInbox, inbox_id)
        return InboxRow.of(row) if row is not None else None


__all__ = ["InboxRow", "load_row"]
```

- [ ] **Step 5: Point `inbound.py` at it**

In `src/personal_organizer/messaging/inbound.py`, delete the `InboxRow` class and `_load`. Remove `dataclass` from the imports if nothing else uses it. Add `from personal_organizer.messaging.inbox import InboxRow, load_row`. Replace `row = await _load(db, inbox_id)` with `row = await load_row(db, inbox_id)`. Keep `"InboxRow"` in `__all__`, which makes it an explicit re-export.

- [ ] **Step 6: Switch the fixture import**

In `tests/fixtures/channels.py`, change `from personal_organizer.messaging.inbound import InboxRow` to `from personal_organizer.messaging.inbox import InboxRow`.

- [ ] **Step 7: Add `add_identity` to PR 2's tenant repository**

Append to `src/personal_organizer/db/repositories/tenants.py`. Add `insert` from `sqlalchemy.dialects.postgresql` and `TenantIdentity` to its imports if absent, and add `"add_identity"` to its `__all__`.

```python
async def add_identity(
    session: AsyncSession, tenant_id: UUID, *, network: str, external_id: str, phone: str | None
) -> None:
    """Record another key for a known tenant (D12). A no-op when the key is already recorded.

    Run inside ``tenant_session(tenant_id)``: the policy's ``WITH CHECK`` is what stops a key
    from being attached to anyone else.
    """
    await session.execute(
        insert(TenantIdentity)
        .values(tenant_id=tenant_id, network=network, external_id=external_id, phone=phone)
        .on_conflict_do_nothing(index_elements=["network", "external_id"])
    )
```

- [ ] **Step 8: Create `messaging/tenancy.py`**

`src/personal_organizer/messaging/tenancy.py`:

```python
"""Who a message is from, as a tenant: the network-keyed identities of D12.

``tenant_identities`` is keyed by **network**, not by gateway. Meta's ``whatsapp`` channel and
the ``gowa`` gateway are both the network ``whatsapp``, so one person writing to either
number is one tenant (ADR 0004's "one person, one key"). A key is ``SenderRef.key``: ``uid:``
plus Meta's BSUID, or ``tel:`` plus the E.164 number.

A sender is looked up by every key their message carries, strongest first. The ``uid:`` key
survives a user hiding their number behind a username, and ``tel:`` is what the gateway and
the invite list know. Two consequences:

- Found by ``tel:`` while the message also carried a ``uid:`` (a Meta BSUID arriving for a
  number first seen on the gateway): the ``uid:`` key is added to that tenant, so their next
  message resolves even with no number in it.
- A new tenant is created on the strongest key, and the others are added at once. Enrolling
  through Meta and writing later through the gateway is therefore still one person.

Resolution and creation run as ``app_user`` through the two ``SECURITY DEFINER`` functions
(D1). The worker cannot read ``tenant_identities`` before it knows the tenant, by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models import NETWORK_WHATSAPP
from personal_organizer.db.repositories.messages import latest_inbound_channel
from personal_organizer.db.repositories.tenants import (
    add_identity,
    create_tenant,
    get_tenant,
    primary_phone,
    resolve_tenant,
)
from personal_organizer.messaging.inbox import InboxRow

#: Channel name -> identity network. A future Telegram channel maps to ``"telegram"``.
_NETWORKS: Final = MappingProxyType({"whatsapp": NETWORK_WHATSAPP, "gowa": NETWORK_WHATSAPP})


@dataclass(frozen=True, slots=True)
class TenantState:
    """What the gate and the onboarding steps decide from, read once per job."""

    id: UUID
    status: str
    step: str | None
    language: str
    #: No inbound message of theirs is recorded yet, so this one is their first. Recording
    #: happens in the transaction that finishes a row (D7), so a job that died before
    #: finishing still counts as the first message when it re-runs.
    first_message: bool


def network_for(channel: str) -> str:
    try:
        return _NETWORKS[channel]
    except KeyError:
        msg = f"no identity network for channel {channel!r}"
        raise ValueError(msg) from None


def identity_keys(row: InboxRow) -> tuple[str, ...]:
    """The row's keys, strongest first. The first is always ``row.sender_key``."""
    keys: list[str] = []
    if row.sender_user_id:
        keys.append(f"uid:{row.sender_user_id}")
    if row.sender_phone:
        keys.append(f"tel:{row.sender_phone}")
    return tuple(keys)


async def _first_match(
    session: AsyncSession, network: str, keys: tuple[str, ...]
) -> tuple[UUID, int] | None:
    for index, key in enumerate(keys):
        tenant_id = await resolve_tenant(session, network=network, external_id=key)
        if tenant_id is not None:
            return tenant_id, index
    return None


async def _link(
    db: Database, tenant_id: UUID, network: str, keys: tuple[str, ...], phone: str | None
) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        for key in keys:
            await add_identity(session, tenant_id, network=network, external_id=key, phone=phone)


async def resolve_sender(db: Database, row: InboxRow) -> UUID | None:
    """The tenant this message is from, or ``None``. Records any stronger key it was missing."""
    network = network_for(row.channel)
    keys = identity_keys(row)
    async with db.system_session() as session:
        found = await _first_match(session, network, keys)
    if found is None:
        return None
    tenant_id, index = found
    if index > 0:
        await _link(db, tenant_id, network, keys[:index], row.sender_phone)
    return tenant_id


async def enrol(db: Database, row: InboxRow, *, language: str) -> UUID:
    """Create the tenant for an invited sender. Idempotent on the identity key."""
    network = network_for(row.channel)
    keys = identity_keys(row)
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=network,
            external_id=keys[0],
            phone=row.sender_phone,
            language=language,
        )
    if len(keys) > 1:
        await _link(db, tenant_id, network, keys[1:], row.sender_phone)
    return tenant_id


async def load_state(db: Database, tenant_id: UUID) -> TenantState | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            return None
        first = await latest_inbound_channel(session, tenant_id) is None
    return TenantState(
        id=tenant.id,
        status=tenant.status,
        step=tenant.onboarding_step,
        language=tenant.language,
        first_message=first,
    )


async def phone_of(db: Database, tenant_id: UUID) -> str | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await primary_phone(session, tenant_id)


__all__ = [
    "TenantState",
    "enrol",
    "identity_keys",
    "load_state",
    "network_for",
    "phone_of",
    "resolve_sender",
]
```

- [ ] **Step 9: Run the new tests and the Iteration 02 gate's**

Run: `uv run pytest tests/unit/test_tenancy.py tests/db/test_tenancy.py tests/db/test_handle_inbound.py tests/db/test_outbox.py -q`
Expected: all pass.

- [ ] **Step 10: Lint, type-check, commit**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: clean.

```bash
git add src/personal_organizer/messaging/inbox.py src/personal_organizer/messaging/tenancy.py \
        src/personal_organizer/messaging/inbound.py src/personal_organizer/db/repositories/tenants.py \
        tests/fixtures/channels.py tests/unit/test_tenancy.py tests/db/test_tenancy.py
git commit -m "feat: resolve senders to tenants by network-keyed identity"
```

---

### Task 8: The onboarding step machine (D9, D10)

**Files:**
- Create: `src/personal_organizer/messaging/onboarding.py`
- Test: `tests/db/test_onboarding_steps.py`

**Interfaces:**
- Consumes: `Choice`, `Option`, `render_text` and `resolve` (Task 1), `guess_zone` and `match_zone` (Task 2), `t` (Task 3), `issue_or_reuse_link` (Task 6), `send_once` (Task 5), and `InboxRow`, `TenantState` and `phone_of` (Task 7).
- Produces:
  - Kinds: `WELCOME_ZONE = "onboarding:welcome_zone"`, `ZONE_ASK_CITY = "onboarding:zone_ask_city"`, `ZONE_RETRY = "onboarding:zone_retry"`, `CONNECT = "onboarding:connect"` and `CONNECT_RESEND = "onboarding:connect_resend"`.
  - Option ids: `ZONE_CORRECT = "zone:correct"` and `ZONE_CHANGE = "zone:change"`.
  - `@dataclass(frozen=True, slots=True) class Advance: timezone: str | None = None; next_step: str | None = None`. The gate commits it, and `None` means unchanged.
  - `zone_choice(zone: str, language: str) -> Choice`, `welcome_text(language: str, guess: str | None) -> str`, `retry_text(language: str, guess: str | None) -> str` and `connect_text(language: str, zone: str, url: str) -> str`.
  - `onboarding_step(db: Database, channel: OutboundChannel, row: InboxRow, tenant: TenantState, *, settings: Settings, now: datetime) -> Advance` sends, writes no tenant state, and returns the change.

The decision table for the `zone` step. Every row is deterministic in `(step, first_message, the guess from the phone, the message)`:

| Situation | Sends (kind) | Returns |
|---|---|---|
| first recorded message | `welcome_zone`: welcome + choice (guess) or welcome + ask city (no guess) | `Advance()` |
| guess, reply resolves to Correct | `connect`: zone set + link | `Advance(guess, "connect")` |
| guess, reply resolves to Change | `zone_ask_city` | `Advance()` |
| text matches a zone (with or without a guess) | `connect`: zone set + link | `Advance(zone, "connect")` |
| anything else (incl. no text) | `zone_retry` (+ "or reply 1 to keep X" with a guess) | `Advance()` |

In the `connect` step (and defensively any other step), any message sends `connect_resend` with the reused or fresh link and returns `Advance()`.

- [ ] **Step 1: Write the failing tests**

`tests/db/test_onboarding_steps.py`:

```python
"""The step machine's decision table, called directly. The gate around it, and committing its
``Advance``, are tested end to end in ``test_onboarding.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.db.engine import Database
from personal_organizer.messaging.choices import render_text
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.onboarding import (
    CONNECT,
    CONNECT_RESEND,
    WELCOME_ZONE,
    ZONE_ASK_CITY,
    ZONE_CHANGE,
    ZONE_CORRECT,
    ZONE_RETRY,
    Advance,
    connect_text,
    onboarding_step,
    retry_text,
    welcome_text,
    zone_choice,
)
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, insert_inbox
from tests.fixtures.tenants import IL_PHONE, NL_PHONE, US_PHONE, link_payload, new_tenant

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables")]

AMSTERDAM = "Europe/Amsterdam"


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


def _state(
    tenant_id: UUID, *, step: str | None = "zone", language: str = "en", first: bool = False
) -> TenantState:
    return TenantState(
        id=tenant_id, status="onboarding", step=step, language=language, first_message=first
    )


async def _step(
    db: Database,
    conn: Any,
    settings: Settings,
    state: TenantState,
    *,
    body: str | None,
    reply_id: str | None = None,
    phone: str | None = NL_PHONE,
    user_id: str | None = None,
    message_type: str = "text",
) -> tuple[Advance, FakeOutbound]:
    channel = FakeOutbound(name="gowa")
    inbox_id = await insert_inbox(
        conn, phone=phone, user_id=user_id, body=body, reply_id=reply_id,
        message_type=message_type,
    )
    row = await load_row(db, inbox_id)
    assert row is not None
    advance = await onboarding_step(
        db, channel, row, state, settings=settings, now=datetime.now(UTC)
    )
    return advance, channel


async def _kinds(conn: Any) -> list[str]:
    return [r["kind"] for r in await conn.fetch("SELECT kind FROM channel_outbox ORDER BY created_at")]


class TestTexts:
    def test_correct_is_option_one(self) -> None:
        """``zone_retry_keep`` says "reply 1"; that is only true while Correct comes first."""
        assert zone_choice(AMSTERDAM, "en").options[0].id == ZONE_CORRECT
        assert zone_choice(AMSTERDAM, "he").options[1].id == ZONE_CHANGE

    def test_the_welcome_with_a_guess(self) -> None:
        assert welcome_text("en", AMSTERDAM) == (
            "Hi! I'm your personal organizer. Two quick steps and you're set up.\n\n"
            "Is your time zone Europe/Amsterdam?\n\n1. Correct\n2. Change\n\n"
            "Reply with a number."
        )

    def test_the_welcome_without_a_guess(self) -> None:
        assert welcome_text("en", None) == (
            f"{t('welcome', 'en')}\n\n{t('zone_ask_city', 'en')}"
        )

    def test_the_hebrew_welcome(self) -> None:
        text = welcome_text("he", "Asia/Jerusalem")
        assert text.endswith(render_text(zone_choice("Asia/Jerusalem", "he"), "he"))
        assert "1. נכון\n2. לשנות" in text

    def test_retry_offers_the_guess(self) -> None:
        assert retry_text("en", AMSTERDAM) == (
            f"{t('zone_retry', 'en')}\nOr reply 1 to keep Europe/Amsterdam."
        )
        assert retry_text("en", None) == t("zone_retry", "en")


class TestZoneStep:
    async def test_the_first_message_gets_the_welcome(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, first=True), body="1"
        )
        # Even "1": a first message is never read as an answer to a question not yet asked.
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [welcome_text("en", AMSTERDAM)]
        assert await _kinds(owner_conn) == [WELCOME_ZONE]

    async def test_no_guess_welcome_asks_for_a_city(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=US_PHONE)
        _, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, first=True),
            body="hi", phone=US_PHONE,
        )
        assert [m.body for m in channel.sent] == [welcome_text("en", None)]

    @pytest.mark.parametrize(
        ("body", "reply_id"),
        [
            ("1", None),
            ("1.", None),
            (" 1) ", None),
            ("Correct", None),
            ("yes!", None),
            ("כן", None),
            ("נכון", None),
            ("1. Correct", ZONE_CORRECT),  # a GOWA selection, or a Meta button
        ],
    )
    async def test_confirming_the_guess_sends_the_link(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        body: str,
        reply_id: str | None,
    ) -> None:
        tenant_id = await new_tenant(db)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body=body, reply_id=reply_id
        )
        assert advance == Advance(timezone=AMSTERDAM, next_step="connect")
        [message] = channel.sent
        assert message.recipient == NL_PHONE
        assert message.body.startswith(t("zone_set", "en", zone=AMSTERDAM))
        assert link_payload(message.body).tenant_id == tenant_id
        assert await _kinds(owner_conn) == [CONNECT]

    @pytest.mark.parametrize(
        ("body", "reply_id"), [("2", None), ("Change", None), ("no", None), ("לא", None),
                               ("2. Change", ZONE_CHANGE)]
    )
    async def test_change_asks_for_a_city(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        body: str,
        reply_id: str | None,
    ) -> None:
        tenant_id = await new_tenant(db)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body=body, reply_id=reply_id
        )
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [t("zone_ask_city", "en")]
        assert await _kinds(owner_conn) == [ZONE_ASK_CITY]

    @pytest.mark.parametrize(
        ("phone", "body", "zone"),
        [
            (NL_PHONE, "London", "Europe/London"),  # a city instead of Correct/Change
            (NL_PHONE, "tel aviv", "Asia/Jerusalem"),
            (US_PHONE, "new york", "America/New_York"),
            (US_PHONE, "Europe/Berlin", "Europe/Berlin"),
        ],
    )
    async def test_a_city_confirms_its_zone(
        self,
        db: Database,
        owner_conn: Any,
        onboarding_settings: Settings,
        phone: str,
        body: str,
        zone: str,
    ) -> None:
        tenant_id = await new_tenant(db, phone=phone)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body=body, phone=phone
        )
        assert advance == Advance(timezone=zone, next_step="connect")
        url = channel.sent[0].body.rsplit("\n", 1)[-1]
        assert channel.sent[0].body == connect_text("en", zone, url)

    async def test_a_digit_means_nothing_when_no_choice_is_open(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=US_PHONE)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body="1", phone=US_PHONE
        )
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [retry_text("en", None)]

    async def test_an_unmatched_reply_gets_the_retry_prompt(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body="Mars"
        )
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [retry_text("en", AMSTERDAM)]
        assert await _kinds(owner_conn) == [ZONE_RETRY]

    async def test_a_message_without_text_gets_the_retry_prompt(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 5: a voice note, a sticker or an image during the zone step."""
        tenant_id = await new_tenant(db)
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body=None,
            message_type="audio",
        )
        assert advance == Advance()
        assert [m.body for m in channel.sent] == [retry_text("en", AMSTERDAM)]

    async def test_hebrew_tenant_hebrew_replies(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=IL_PHONE, language="he")
        _, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, language="he"),
            body="מאדים", phone=IL_PHONE,
        )
        assert [m.body for m in channel.sent] == [retry_text("he", "Asia/Jerusalem")]


class TestConnectStep:
    @pytest.mark.parametrize("step", ["connect", None])
    async def test_any_message_resends_the_link(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings, step: str | None
    ) -> None:
        tenant_id = await new_tenant(db, step="connect")
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id, step=step), body="hello?"
        )
        assert advance == Advance()
        [message] = channel.sent
        url = message.body.rsplit("\n", 1)[-1]
        assert message.body == t("connect_link", "en", url=url)
        assert link_payload(message.body).tenant_id == tenant_id
        assert await _kinds(owner_conn) == [CONNECT_RESEND]


class TestAddressing:
    async def test_a_bsuid_only_message_goes_to_the_tenants_number(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db)
        _, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body="Mars",
            phone=None, user_id="US.1",
        )
        assert [m.recipient for m in channel.sent] == [NL_PHONE]

    async def test_no_number_anywhere_sends_nothing(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        tenant_id = await new_tenant(db, phone=None, user_id="US.9")
        advance, channel = await _step(
            db, owner_conn, onboarding_settings, _state(tenant_id), body="1",
            phone=None, user_id="US.9",
        )
        assert advance == Advance()
        assert channel.attempts == []
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/db/test_onboarding_steps.py -q`
Expected: `ModuleNotFoundError: No module named 'personal_organizer.messaging.onboarding'`.

- [ ] **Step 3: Implement**

`src/personal_organizer/messaging/onboarding.py`:

```python
"""The onboarding conversation: the ``zone`` and ``connect`` steps (D10).

The tenant gate calls this for every message from an ``onboarding`` tenant. It decides from
four things only: the tenant's stored step, whether this is their first recorded message,
the guess from their number, and the message itself. None of those change until the gate
commits, so a re-run of the job reaches the same decision.

**Send first, return the change.** The step sends through :func:`send_once` under one kind
per decision, and *returns* an :class:`Advance` rather than writing it. The gate commits the
advance together with the finished inbox row and its copy into ``messages`` (D7), in one
transaction. Consider a job that dies after a send and before that commit. It re-runs into a
send that is already claimed and an advance that has not happened, and the step moves once.
The reverse order, writing the step first, would make the re-run treat the same message as
a ``connect``-step message and send the link a second time.

**No choice is stored (D9).** While the step is ``zone`` and the number's country gives a
guess, the Correct/Change choice is open, rebuilt from the guess on every message. There is
no separate "waiting for a city" state either. A city is accepted whenever it is sent, so
"Change" only has to ask for one. One consequence: "1" after "Change" still means Correct.
That is harmless, because the user is shown the zone they are keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog

from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.choices import Choice, Option, render_text, resolve
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.tenancy import TenantState, phone_of
from personal_organizer.messaging.timezones import guess_zone, match_zone
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

WELCOME_ZONE: Final = "onboarding:welcome_zone"
ZONE_ASK_CITY: Final = "onboarding:zone_ask_city"
ZONE_RETRY: Final = "onboarding:zone_retry"
CONNECT: Final = "onboarding:connect"
CONNECT_RESEND: Final = "onboarding:connect_resend"

ZONE_CORRECT: Final = "zone:correct"
ZONE_CHANGE: Final = "zone:change"

ZONE_STEP: Final = "zone"
CONNECT_STEP: Final = "connect"

#: Both languages, whatever the tenant's: people answer in the language they think in.
_CORRECT_ALIASES: Final = ("yes", "y", "ok", "correct", "right", "כן", "נכון")
_CHANGE_ALIASES: Final = ("no", "n", "change", "wrong", "לא", "לשנות", "שנה")


@dataclass(frozen=True, slots=True)
class Advance:
    """The state change a step decided, for the gate to commit with the finished row.
    ``None`` leaves a field as it is."""

    timezone: str | None = None
    next_step: str | None = None


def zone_choice(zone: str, language: str) -> Choice:
    """Correct first: ``zone_retry_keep`` tells the user to reply 1 to keep the guess."""
    return Choice(
        prompt=t("zone_confirm", language, zone=zone),
        options=(
            Option(ZONE_CORRECT, t("option_correct", language), _CORRECT_ALIASES),
            Option(ZONE_CHANGE, t("option_change", language), _CHANGE_ALIASES),
        ),
    )


def welcome_text(language: str, guess: str | None) -> str:
    ask = render_text(zone_choice(guess, language), language) if guess else t(
        "zone_ask_city", language
    )
    return f"{t('welcome', language)}\n\n{ask}"


def retry_text(language: str, guess: str | None) -> str:
    retry = t("zone_retry", language)
    return f"{retry}\n{t('zone_retry_keep', language, zone=guess)}" if guess else retry


def connect_text(language: str, zone: str, url: str) -> str:
    return f"{t('zone_set', language, zone=zone)}\n\n{t('connect_link', language, url=url)}"


async def onboarding_step(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    tenant: TenantState,
    *,
    settings: Settings,
    now: datetime,
) -> Advance:
    """Answer ``row`` for an onboarding ``tenant``. Sends; writes no tenant state."""
    address = row.sender_phone or await phone_of(db, tenant.id)
    if address is None:
        # A BSUID-only sender whose tenant has no number on file. Whether Graph accepts a
        # BSUID as ``to`` is unconfirmed, so there is no reply, exactly as for strangers.
        log.info("onboarding.unaddressable", inbox_id=str(row.id), tenant_id=str(tenant.id))
        return Advance()
    to: str = address
    language = tenant.language
    guess = guess_zone(to)

    async def send(kind: str, text: str) -> None:
        await send_once(
            db, channel, inbox_id=row.id, kind=kind, recipient_key=row.sender_key, to=to,
            text=text,
        )

    async def confirm(zone: str) -> Advance:
        url = await issue_or_reuse_link(db, tenant.id, settings=settings, now=now)
        await send(CONNECT, connect_text(language, zone, url))
        return Advance(timezone=zone, next_step=CONNECT_STEP)

    if tenant.step != ZONE_STEP:
        # ``connect``, and defensively anything else: connecting is the only way out.
        url = await issue_or_reuse_link(db, tenant.id, settings=settings, now=now)
        await send(CONNECT_RESEND, t("connect_link", language, url=url))
        return Advance()
    if tenant.first_message:
        await send(WELCOME_ZONE, welcome_text(language, guess))
        return Advance()
    if guess is not None:
        picked = resolve(zone_choice(guess, language), reply_id=row.reply_id, text=row.body)
        if picked is not None and picked.id == ZONE_CORRECT:
            return await confirm(guess)
        if picked is not None:
            await send(ZONE_ASK_CITY, t("zone_ask_city", language))
            return Advance()
    zone = match_zone(row.body) if row.body else None
    if zone is not None:
        return await confirm(zone)
    log.info("onboarding.zone_unmatched", inbox_id=str(row.id), tenant_id=str(tenant.id))
    await send(ZONE_RETRY, retry_text(language, guess))
    return Advance()


__all__ = [
    "CONNECT",
    "CONNECT_RESEND",
    "CONNECT_STEP",
    "WELCOME_ZONE",
    "ZONE_ASK_CITY",
    "ZONE_CHANGE",
    "ZONE_CORRECT",
    "ZONE_RETRY",
    "ZONE_STEP",
    "Advance",
    "connect_text",
    "onboarding_step",
    "retry_text",
    "welcome_text",
    "zone_choice",
]
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/db/test_onboarding_steps.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: clean.

```bash
git add src/personal_organizer/messaging/onboarding.py tests/db/test_onboarding_steps.py
git commit -m "feat: the onboarding zone and connect steps"
```

---

### Task 9: The tenant gate in `handle_inbound`, D7, and the worker wiring (D2, D7)

**Files:**
- Modify: `src/personal_organizer/messaging/inbound.py`
- Modify: `src/personal_organizer/worker/tasks/channel.py`
- Test: `tests/db/test_onboarding.py`
- Unchanged and must stay green: `tests/db/test_handle_inbound.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8, plus `record_inbound` (PR 2, `db/repositories/messages.py`) and `set_timezone` and `set_onboarding_step` (PR 2, `db/repositories/tenants.py`).
- Produces: `handle_inbound(inbox_id, *, db, channels, allowlist, on_allowed, settings: Settings | None = None, now=_utcnow) -> str | None`. Dispositions are `allowed`, `onboarding`, `stranger`, `stranger_muted` and `stale`. `record_inbound` is imported **by name** into `messaging.inbound`, which the crash test patches.

- [ ] **Step 1: Write the failing end-to-end tests**

`tests/db/test_onboarding.py`:

```python
"""Onboarding over chat, end to end against Postgres, with fake channels.

Done-When (the chat half): an invited number writes, confirms its time zone with a numbered
reply and is sent a connect link. Also pinned:
- D2: who is served, onboarded, enrolled or turned away;
- D7: a known tenant's words end up under RLS and nowhere else;
- that no re-run of any job sends twice or advances twice.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from personal_organizer.core.errors import TransientChannelError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models import NETWORK_WHATSAPP
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.db.repositories.tenants import resolve_tenant
from personal_organizer.interfaces.channel import OutboundChannel, OutboundMessage
from personal_organizer.messaging import inbound
from personal_organizer.messaging.inbound import InboxRow, acknowledge, handle_inbound
from personal_organizer.messaging.onboarding import welcome_text
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.replies import ACK_TEXT, INVITE_ONLY_TEXT
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.tenants import (
    BASE_URL,
    IL_PHONE,
    NL_PHONE,
    US_PHONE,
    link_payload,
    links_of,
    messages_of,
    new_tenant,
    tenant_by_phone,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables")]

STRANGER_PHONE = "+447700900123"
UNINVITED_TENANT_PHONE = "+4915112345678"
INVITED = frozenset({IL_PHONE, NL_PHONE, US_PHONE})


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


@dataclass
class Person:
    """One person writing to the bot: each ``say`` stores a message as ingress would and runs
    its job as the worker would. Both channels are live, and replies follow the inbound one."""

    db: Database
    conn: Any
    settings: Settings
    phone: str | None = NL_PHONE
    user_id: str | None = None
    gowa: FakeOutbound = field(default_factory=lambda: FakeOutbound(name="gowa"))
    meta: FakeOutbound = field(default_factory=lambda: FakeOutbound(name="whatsapp"))
    agent: Spy = field(default_factory=Spy)
    last: UUID | None = None

    async def say(
        self,
        body: str | None,
        *,
        via: str = "gowa",
        reply_id: str | None = None,
        message_type: str = "text",
        sent_at: datetime | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> str | None:
        self.last = await insert_inbox(
            self.conn, phone=self.phone, user_id=self.user_id, body=body, reply_id=reply_id,
            message_type=message_type, channel=via, sent_at=sent_at,
        )
        return await self.run(self.last, now=now)

    async def run(
        self, inbox_id: UUID, *, now: Callable[[], datetime] | None = None
    ) -> str | None:
        return await handle_inbound(
            inbox_id,
            db=self.db,
            channels={self.gowa.name: self.gowa, self.meta.name: self.meta}.__getitem__,
            allowlist=INVITED,
            on_allowed=self.agent,
            settings=self.settings,
            now=now or (lambda: datetime.now(UTC)),
        )

    def texts(self) -> list[str]:
        return [message.body for message in self.gowa.sent + self.meta.sent]


@pytest.fixture
def person(db: Database, owner_conn: Any, onboarding_settings: Settings) -> Person:
    return Person(db, owner_conn, onboarding_settings)


async def _inbox(conn: Any, inbox_id: UUID | None) -> Any:
    return await conn.fetchrow("SELECT * FROM channel_inbox WHERE id = $1", inbox_id)


async def _kinds(conn: Any) -> list[str]:
    rows = await conn.fetch("SELECT kind FROM channel_outbox ORDER BY created_at")
    return [row["kind"] for row in rows]


def _fail_finishing_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The database goes away inside the transaction that finishes the row."""
    real = inbound.record_inbound
    calls: list[int] = []

    async def flaky(*args: Any, **kwargs: Any) -> UUID:
        calls.append(1)
        if len(calls) == 1:
            msg = "the database went away at the commit"
            raise ConnectionResetError(msg)
        return await real(*args, **kwargs)

    monkeypatch.setattr(inbound, "record_inbound", flaky)


class TestFirstMessage:
    async def test_an_invited_number_becomes_an_onboarding_tenant(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        person = Person(db, owner_conn, onboarding_settings, phone=IL_PHONE)
        assert await person.say("שלום!") == "onboarding"

        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert (tenant.status, tenant.onboarding_step, tenant.language, tenant.timezone) == (
            "onboarding", "zone", "he", None,
        )
        assert person.texts() == [welcome_text("he", "Asia/Jerusalem")]
        assert person.gowa.read != []
        assert person.agent.calls == []
        assert await _kinds(owner_conn) == ["onboarding:welcome_zone"]

    @pytest.mark.parametrize(("body", "language"), [("hello", "en"), (None, "en"), ("hi שלום", "he")])
    async def test_the_language_comes_from_the_words_not_the_number(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings, body: str | None,
        language: str,
    ) -> None:
        person = Person(db, owner_conn, onboarding_settings, phone=IL_PHONE)
        await person.say(body, message_type="text" if body else "sticker")
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert tenant.language == language

    async def test_its_content_moves_under_rls(self, person: Person, db: Database) -> None:
        await person.say("Oncology appointment with Dr Meyer")
        row = await _inbox(person.conn, person.last)
        assert (row["body"], row["raw"], row["disposition"]) == (None, None, "onboarding")
        assert row["processed_at"] is not None
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        [message] = await messages_of(db, tenant.id)
        assert (message.direction, message.channel, message.inbox_id, message.body) == (
            "in", "gowa", person.last, "Oncology appointment with Dr Meyer",
        )

    async def test_a_tenant_left_by_a_crashed_job_still_gets_the_welcome(
        self, person: Person, db: Database
    ) -> None:
        """The previous run created the tenant and died: the re-run must welcome, not retry."""
        await new_tenant(db)
        await person.say("hi")
        assert person.texts() == [welcome_text("en", "Europe/Amsterdam")]

    async def test_a_stale_first_message_creates_no_tenant(
        self, person: Person, db: Database
    ) -> None:
        sent_at = datetime.now(UTC) - timedelta(hours=30)
        assert await person.say("hi", sent_at=sent_at) == "stale"
        assert await tenant_by_phone(db, NL_PHONE) is None
        assert person.gowa.attempts == []
        assert (await _inbox(person.conn, person.last))["body"] is None


class TestComposioOff:
    async def test_iteration_02_exactly(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        """Before Composio is configured, invited senders keep the acknowledgement."""
        channel = FakeOutbound(name="gowa")
        inbox_id = await insert_inbox(owner_conn)

        async def ack(row: InboxRow, resolved: OutboundChannel) -> None:
            await acknowledge(row, resolved, db=db)

        disposition = await handle_inbound(
            inbox_id, db=db, channels={"gowa": channel}.__getitem__, allowlist=INVITED,
            on_allowed=ack, settings=db_settings,
        )
        assert disposition == "allowed"
        assert channel.sent == [OutboundMessage(recipient=NL_PHONE, body=ACK_TEXT)]
        assert await tenant_by_phone(db, NL_PHONE) is None
        assert (await _inbox(owner_conn, inbox_id))["body"] == "hi"


class TestStrangers:
    async def test_unchanged_with_composio_on(self, db: Database, owner_conn: Any,
                                              onboarding_settings: Settings) -> None:
        stranger = Person(db, owner_conn, onboarding_settings, phone=STRANGER_PHONE)
        assert await stranger.say("hello") == "stranger"
        assert stranger.texts() == [INVITE_ONLY_TEXT]
        assert stranger.agent.calls == []
        assert await tenant_by_phone(db, STRANGER_PHONE) is None
        assert (await _inbox(owner_conn, stranger.last))["body"] is None

    async def test_a_suspended_tenant_is_turned_away(
        self, person: Person, db: Database
    ) -> None:
        await new_tenant(db, status="suspended")
        assert await person.say("hello") == "stranger"
        assert person.texts() == [INVITE_ONLY_TEXT]
        assert person.agent.calls == []
        assert (await _inbox(person.conn, person.last))["body"] is None


class TestZoneToConnect:
    async def test_the_whole_chat(self, person: Person, db: Database) -> None:
        await person.say("hi")
        assert await person.say("1") == "onboarding"

        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == ("Europe/Amsterdam", "connect")
        first_link = link_payload(person.texts()[-1])
        assert first_link.tenant_id == tenant.id

        await person.say("did it work?")
        resent = person.texts()[-1]
        assert resent == t("connect_link", "en", url=resent.rsplit("\n", 1)[-1])
        assert link_payload(resent).nonce == first_link.nonce
        assert len(await links_of(db, tenant.id)) == 1
        assert await _kinds(person.conn) == [
            "onboarding:welcome_zone", "onboarding:connect", "onboarding:connect_resend",
        ]
        assert len(await messages_of(db, tenant.id)) == 3

    async def test_change_then_a_city(self, person: Person, db: Database) -> None:
        await person.say("hi")
        await person.say("2")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == (None, "zone")

        await person.say("London")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.timezone, tenant.onboarding_step) == ("Europe/London", "connect")

    async def test_a_used_or_aging_link_is_replaced(self, person: Person, db: Database) -> None:
        await person.say("hi")
        await person.say("1")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        first = link_payload(person.texts()[-1]).nonce
        async with db.tenant_session(TenantId(tenant.id)) as session:
            await consume_link(session, tenant.id, nonce=first, now=datetime.now(UTC))

        await person.say("the link didn't work")
        second = link_payload(person.texts()[-1]).nonce
        assert second != first

        ttl = person.settings.onboarding.link_ttl_s
        later = datetime.now(UTC) + timedelta(seconds=ttl * 0.6)
        await person.say("still nothing", now=lambda: later)
        assert link_payload(person.texts()[-1]).nonce not in (first, second)


class TestReRuns:
    async def test_a_transient_failure_neither_advances_nor_double_sends(
        self, person: Person, db: Database
    ) -> None:
        await person.say("hi")
        person.gowa.failures.append(TransientChannelError("429"))
        with pytest.raises(TransientChannelError):
            await person.say("1")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert tenant.onboarding_step == "zone"
        assert person.last is not None

        assert await person.run(person.last) == "onboarding"
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert tenant.onboarding_step == "connect"
        assert len(person.gowa.sent) == 2  # the welcome, then one link
        assert len(await links_of(db, tenant.id)) == 1

    async def test_a_crash_after_the_send_neither_resends_nor_advances_twice(
        self, person: Person, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review Focus 1: the link went out, then the job died before committing the step."""
        await person.say("hi")
        _fail_finishing_once(monkeypatch)
        with pytest.raises(ConnectionResetError):
            await person.say("1")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.onboarding_step, tenant.timezone) == ("zone", None)
        assert (await _inbox(person.conn, person.last))["processed_at"] is None
        assert person.last is not None

        assert await person.run(person.last) == "onboarding"
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert (tenant.onboarding_step, tenant.timezone) == ("connect", "Europe/Amsterdam")
        assert len(person.gowa.sent) == 2
        assert await _kinds(person.conn) == ["onboarding:welcome_zone", "onboarding:connect"]
        assert len(await messages_of(db, tenant.id)) == 2

    async def test_a_finished_row_is_a_no_op(self, person: Person) -> None:
        await person.say("hi")
        assert person.last is not None
        assert await person.run(person.last) == "onboarding"
        assert len(person.gowa.sent) == 1


class TestActiveTenants:
    async def test_handed_to_the_agent_and_kept_under_rls(
        self, person: Person, db: Database
    ) -> None:
        tenant_id = await new_tenant(db, status="active")
        assert await person.say("Oncology appointment with Dr Meyer") == "allowed"
        assert [row.id for row in person.agent.calls] == [person.last]
        assert person.agent.calls[0].body == "Oncology appointment with Dr Meyer"
        assert person.gowa.read != []
        assert person.texts() == []  # the Spy stands in for the acknowledgement
        assert (await _inbox(person.conn, person.last))["body"] is None
        [message] = await messages_of(db, tenant_id)
        assert message.body == "Oncology appointment with Dr Meyer"

    async def test_identity_wins_over_the_invite_list(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Onboarded people stay served after their number leaves WHATSAPP__ALLOWED_PHONES."""
        await new_tenant(db, phone=UNINVITED_TENANT_PHONE, status="active")
        person = Person(db, owner_conn, onboarding_settings, phone=UNINVITED_TENANT_PHONE)
        assert await person.say("hello") == "allowed"
        assert len(person.agent.calls) == 1

    async def test_a_stale_message_is_kept_but_not_answered(
        self, person: Person, db: Database
    ) -> None:
        tenant_id = await new_tenant(db, status="active")
        sent_at = datetime.now(UTC) - timedelta(hours=30)
        assert await person.say("from yesterday", sent_at=sent_at) == "stale"
        assert person.agent.calls == []
        assert person.gowa.attempts == []
        assert (await _inbox(person.conn, person.last))["body"] is None
        assert [m.body for m in await messages_of(db, tenant_id)] == ["from yesterday"]


class TestOnePersonTwoChannels:
    async def test_enrolled_on_the_gateway_continued_on_meta(
        self, person: Person, db: Database
    ) -> None:
        await person.say("hi", via="gowa")
        await person.say("1", via="whatsapp")
        assert len(person.gowa.sent) == 1
        assert len(person.meta.sent) == 1  # the reply follows the inbound channel
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert tenant.onboarding_step == "connect"

    async def test_a_bsuid_then_a_hidden_number_stay_one_tenant(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 3, through the gate."""
        meta = Person(db, owner_conn, onboarding_settings, user_id="US.1")
        await meta.say("hi", via="whatsapp")
        hidden = Person(db, owner_conn, onboarding_settings, phone=None, user_id="US.1",
                        meta=meta.meta)
        assert await hidden.say("1", via="whatsapp") == "onboarding"
        gateway = Person(db, owner_conn, onboarding_settings, gowa=meta.gowa, meta=meta.meta)
        assert await gateway.say("hello", via="gowa") == "onboarding"

        async with db.system_session() as session:
            by_uid = await resolve_tenant(session, network=NETWORK_WHATSAPP, external_id="uid:US.1")
        tenant = await tenant_by_phone(db, NL_PHONE)
        assert tenant is not None
        assert by_uid == tenant.id
        assert tenant.onboarding_step == "connect"
        assert len(await messages_of(db, tenant.id)) == 3


class TestLogs:
    async def test_onboarding_logs_neither_who_nor_what_nor_the_link(
        self, person: Person, settings: Settings
    ) -> None:
        """Review Focus 4: the link is a bearer credential for the connect page."""
        stream = io.StringIO()
        configure_logging(settings, stream=stream)
        await person.say("Oncology appointment with Dr Meyer")
        await person.say("Mars")
        await person.say("1")
        await person.say("again please")

        captured = stream.getvalue()
        assert "inbound.handled" in captured
        token = person.texts()[-1].rsplit("/", 1)[-1]
        for leaked in (NL_PHONE, "31612345678", "Oncology appointment", "Mars", BASE_URL, token):
            assert leaked not in captured
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/db/test_onboarding.py -q`
Expected: failures with `TypeError: handle_inbound() got an unexpected keyword argument 'settings'`.

- [ ] **Step 3: Implement the gate**

Rewrite `src/personal_organizer/messaging/inbound.py`. Everything below replaces the module, and `acknowledge`, `_already_told` and `_turn_away` keep their current bodies.

```python
"""Handling one stored inbound message: the gate in front of the agent.

Two gates, chosen by configuration. With Composio off, before the Google connect is set up
on an environment, it is Iteration 02's allowlist gate, unchanged. An invited sender gets the
fixed acknowledgement. Anyone else gets one invite-only line a day, and their content is
deleted. With Composio on it is the tenant gate (D2 in docs/plan-iteration-03.md):

====================================  ======================================================
Sender                                Outcome (disposition)
====================================  ======================================================
resolves to an ``active`` tenant      ``on_allowed`` (``allowed``)
resolves to an ``onboarding`` tenant  the current onboarding step (``onboarding``)
no tenant, number on the invite list  ``enrol``, then the first step (``onboarding``)
anything else, suspended included     invite-only reply and purge (``stranger[_muted]``)
====================================  ======================================================

What both guarantee is the part that must hold before there *is* an agent. A sender who is
not invited never reaches ``on_allowed``, the seam Iteration 04 turns into "defer an agent
turn", and so never spends an LLM call. Identity wins over the invite list: an onboarded
person stays served after their number is removed from it.

**D7.** A known tenant's message is copied into ``messages``, under RLS, and nulled here.
That happens in the one transaction that finishes the row, together with whatever state the
onboarding step decided. Nothing a known tenant writes outlives the job outside RLS.
Strangers' messages are purged as before. So are those of an invited number whose first
message came too late to answer, since there is no tenant to keep them under.

Everything is idempotent under a re-run of the job. ``processed_at`` ends a re-run early,
every reply goes through :func:`send_once`, and a step's state is committed only *after*
its replies are claimed (see ``messaging.onboarding``).

Replies go out on the channel the message came in on. Several channels can be live at once,
so the handler is given a resolver and looks up the row's ``channel`` by name. The
invite-only mute, though, is per *person* (``recipient_key``), not per channel: a stranger
who writes to two of our numbers is told once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import exists, func, select, update

from personal_organizer.core.errors import ChannelError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import set_onboarding_step, set_timezone
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.inbox import InboxRow, load_row
from personal_organizer.messaging.language import detect_language
from personal_organizer.messaging.onboarding import Advance, onboarding_step
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.replies import (
    ACK_TEXT,
    INVITE_ONLY_MUTE,
    INVITE_ONLY_TEXT,
    SERVICE_WINDOW,
    SERVICE_WINDOW_MARGIN,
)
from personal_organizer.messaging.tenancy import enrol, load_state, resolve_sender
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

ACK: Final = "ack"
INVITE_ONLY: Final = "invite_only"

#: Outbox states that count as "we already told this stranger". A reply that is still
#: pending or that Meta refused does not mute the next one.
_MUTING_STATUSES: Final = ("sending", "unknown", "accepted", "sent", "delivered", "read")

#: Tenant statuses the gate serves. ``suspended`` (and anything newer) is turned away.
_SERVED: Final = frozenset({"active", "onboarding"})

#: What a purge nulls: the message's content, leaving what deduplication and the mute need.
_PURGED: Final = MappingProxyType(
    {"body": None, "raw": None, "media_id": None, "media_mime_type": None}
)

#: The handoff for an allowed sender, given the channel to answer on.
OnAllowed = Callable[[InboxRow, OutboundChannel], Awaitable[None]]

#: Looks up an outbound channel by name; raises ``ChannelNotConfiguredError`` if it is absent.
ChannelResolver = Callable[[str], OutboundChannel]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# _already_told: unchanged from Iteration 02.


async def _finish(db: Database, row: InboxRow, disposition: str, *, purge: bool) -> None:
    values: dict[str, object] = {"processed_at": func.now(), "disposition": disposition}
    if purge:
        # A stranger's words have no reason to be kept, and every reason not to be.
        values |= _PURGED
    async with db.system_session() as session:
        await session.execute(update(ChannelInbox).where(ChannelInbox.id == row.id).values(values))


async def _finish_known(
    db: Database,
    row: InboxRow,
    tenant_id: UUID,
    disposition: str,
    advance: Advance | None = None,
) -> None:
    """D7: copy the content under RLS and null it here, in the transaction that finishes the
    row, together with the state the onboarding step decided."""
    change = advance or Advance()
    async with db.tenant_session(TenantId(tenant_id)) as session:
        if change.timezone is not None:
            await set_timezone(session, tenant_id, change.timezone)
        if change.next_step is not None:
            await set_onboarding_step(session, tenant_id, change.next_step)
        await record_inbound(
            session,
            tenant_id,
            inbox_id=row.id,
            channel=row.channel,
            message_type=row.message_type,
            body=row.body,
            sent_at=row.sent_at,
        )
        await session.execute(
            update(ChannelInbox)
            .where(ChannelInbox.id == row.id)
            .values(processed_at=func.now(), disposition=disposition, **_PURGED)
        )


# acknowledge: unchanged from Iteration 02.
# _turn_away: unchanged from Iteration 02.


async def _mark_read(channel: OutboundChannel, row: InboxRow) -> None:
    try:
        await channel.mark_read(row.provider_message_id)
    except ChannelError as exc:
        # Blue ticks are cosmetic; failing the job over them would delay the reply.
        log.warning(
            "inbound.mark_read_failed", inbox_id=str(row.id), error_type=type(exc).__name__
        )


def _is_stale(row: InboxRow, current: datetime) -> bool:
    # Redelivered after an outage. The reply window has closed, so any send would be
    # refused -- and replying to a day-old message out of the blue is worse than silence.
    return current - row.sent_at >= SERVICE_WINDOW - SERVICE_WINDOW_MARGIN


async def _allowlist_gate(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    current: datetime,
    *,
    allowlist: frozenset[str],
    on_allowed: OnAllowed,
) -> str:
    """Iteration 02's gate, kept exactly for environments without Composio."""
    allowed = row.sender_phone is not None and row.sender_phone in allowlist
    if _is_stale(row, current):
        disposition = "stale"
    elif allowed:
        await _mark_read(channel, row)
        await on_allowed(row, channel)
        disposition = "allowed"
    else:
        disposition = await _turn_away(db, channel, row, current)
    await _finish(db, row, disposition, purge=not allowed)
    return disposition


async def _tenant_gate(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    current: datetime,
    *,
    allowlist: frozenset[str],
    on_allowed: OnAllowed,
    settings: Settings,
) -> str:
    """D2: serve, onboard, enrol or turn away. See the module docstring's table."""
    stale = _is_stale(row, current)
    tenant_id = await resolve_sender(db, row)
    invited = row.sender_phone is not None and row.sender_phone in allowlist
    if tenant_id is None and invited and not stale:
        # Their first message. Its words decide the tenant's language (D11).
        tenant_id = await enrol(db, row, language=detect_language(row.body))
    tenant = await load_state(db, tenant_id) if tenant_id is not None else None

    if tenant is None or tenant.status not in _SERVED:
        disposition = "stale" if stale else await _turn_away(db, channel, row, current)
        await _finish(db, row, disposition, purge=True)
        return disposition
    if stale:
        await _finish_known(db, row, tenant.id, "stale")
        return "stale"
    await _mark_read(channel, row)
    if tenant.status == "active":
        await on_allowed(row, channel)
        await _finish_known(db, row, tenant.id, "allowed")
        return "allowed"
    advance = await onboarding_step(db, channel, row, tenant, settings=settings, now=current)
    await _finish_known(db, row, tenant.id, "onboarding", advance)
    return "onboarding"


async def handle_inbound(
    inbox_id: UUID,
    *,
    db: Database,
    channels: ChannelResolver,
    allowlist: frozenset[str],
    on_allowed: OnAllowed,
    settings: Settings | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> str | None:
    """Process one inbox row. Returns its disposition, or ``None`` if the row is gone.

    ``settings`` with ``composio.enabled`` selects the tenant gate. ``None``, or Composio
    off, is Iteration 02's allowlist gate exactly.
    """
    row = await load_row(db, inbox_id)
    if row is None:
        log.warning("inbound.missing", inbox_id=str(inbox_id))
        return None
    if row.processed_at is not None:
        return row.disposition
    # Before anything is claimed or sent: a row whose channel this worker does not serve
    # (switched off with jobs still queued) fails here and stays unprocessed for a re-run.
    channel = channels(row.channel)

    current = now()
    if settings is not None and settings.composio.enabled:
        disposition = await _tenant_gate(
            db, channel, row, current, allowlist=allowlist, on_allowed=on_allowed,
            settings=settings,
        )
    else:
        disposition = await _allowlist_gate(
            db, channel, row, current, allowlist=allowlist, on_allowed=on_allowed
        )
    log.info(
        "inbound.handled",
        inbox_id=str(row.id),
        channel=row.channel,
        message_type=row.message_type,
        disposition=disposition,
        sender=row.sender_key,
    )
    return disposition


__all__ = [
    "ACK",
    "INVITE_ONLY",
    "ChannelResolver",
    "InboxRow",
    "OnAllowed",
    "acknowledge",
    "handle_inbound",
]
```

The two `# ... unchanged` comments mark where to keep the existing `_already_told`, `acknowledge` and `_turn_away` definitions **verbatim**. Do not leave the comments in the file.

- [ ] **Step 4: Run the new tests and the Iteration 02 tests**

Run: `uv run pytest tests/db/test_onboarding.py tests/db/test_handle_inbound.py -q`
Expected: all pass. If `test_handle_inbound.py` fails, the allowlist gate drifted from Iteration 02. Compare `_allowlist_gate` with the old `handle_inbound` body line by line. Do not edit the old tests.

- [ ] **Step 5: Wire the worker**

In `src/personal_organizer/worker/tasks/channel.py`, replace the task body:

```python
    async def handle_inbound_task(inbox_id: str) -> None:
        db = get_database()
        settings = get_settings()
        await handle_inbound(
            UUID(inbox_id),
            db=db,
            channels=get_outbound_channel,
            # The invite list, whichever channel the message came in on.
            allowlist=settings.whatsapp.allowlist,
            on_allowed=functools.partial(acknowledge, db=db),
            # Composio on: the tenant gate and onboarding. Off: Iteration 02's gate, exactly.
            settings=settings,
        )
```

- [ ] **Step 6: The whole suite, lint and types**

Run: `uv run pytest -q && uv run ruff format src tests && uv run ruff check src tests && uv run mypy`
Expected: everything passes with the database up, and no DB test reports as skipped (`uv run pytest -q -rs` lists skips). ruff and mypy are clean.

- [ ] **Step 7: Commit**

```bash
git add src/personal_organizer/messaging/inbound.py src/personal_organizer/worker/tasks/channel.py \
        tests/db/test_onboarding.py
git commit -m "feat: the tenant gate -- enrol, onboard or serve, with content under RLS"
```

---

## Self-review notes (for the reviewer of this plan)

- **Spec coverage.**
  - D2: Task 9 (`TestFirstMessage`, `TestStrangers`, `TestActiveTenants`, `TestComposioOff`).
  - D6: Task 5.
  - D7: Task 9 (`_finish_known`, the content tests).
  - D9: Task 1, plus Task 8's use.
  - D10: Tasks 2, 6 and 8.
  - D11: Tasks 1 and 3, and Task 9's language test.
  - D12: Task 7, and Task 9's `TestOnePersonTwoChannels`.
  - Done-When "a choice works on every channel": `tests/unit/test_choices.py`.
  - The PR 4 seams: `tokens.py`, `links.py`, `t("connect_link")`, `t("all_set")` and `send_once(idempotency_key=)`.
- **Not in this PR (by the contract):** `/connect/*`, the callback, `onboarding:connected` and "You're all set" (PR 4); the GOWA health check and the docs (PR 5). The ADR 0003 bullet is here because it documents a behaviour this PR adds.
- **Ambiguities resolved:**
  - A suspended tenant is "anything else" in D2's table: invite-only reply and purge.
  - An invited number whose first message is stale creates no tenant, and its content is purged, because there is no tenant to keep it under RLS. Iteration 02 used to keep an allowlisted sender's stale body.
  - "1" while no choice is open (no guess) is free text, which matches no zone, so the user gets the retry prompt.
  - A city typed in place of Correct/Change is accepted.
  - The tenant's language comes from the first message's words, not its number.
