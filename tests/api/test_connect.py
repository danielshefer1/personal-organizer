"""``/connect`` without a database: everything refused before one is reached.

The fake database has no ``tenant_session``, so a request that reached it would answer 500
instead of the page asserted. That is how "refused before touching the database" is pinned.
The flow against Postgres is ``tests/db/test_connect.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from structlog.testing import capture_logs

from personal_organizer.api.app import create_app
from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.onboarding.page_text import page_text
from personal_organizer.settings import Settings
from tests.api.conftest import FakeDatabase, FakeProcrastinate
from tests.fixtures.connect import (
    ACCOUNT_ID,
    CONNECT_ENV,
    OTHER_SECRET,
    FakeConnectLinker,
    account,
    assert_hardened,
    assert_page,
    connect_app,
    link_token,
    sentry_events,
    stale,
    state_token,
)


@pytest.fixture
def connect_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**CONNECT_ENV)


@pytest.fixture
def linker() -> FakeConnectLinker:
    return FakeConnectLinker()


@pytest.fixture
def queue() -> FakeProcrastinate:
    return FakeProcrastinate()


@pytest.fixture
async def http(
    connect_settings: Settings,
    linker: FakeConnectLinker,
    queue: FakeProcrastinate,
    make_client: Callable[[FastAPI], AsyncClient],
) -> AsyncIterator[AsyncClient]:
    application = connect_app(connect_settings, db=FakeDatabase(), linker=linker, queue=queue)
    async with make_client(application) as client:
        yield client


#: Refused before the database is asked. An expired link that is still ours is looked up (an
#: active tenant gets "already connected"), so it is covered in tests/db/test_connect.py, as is
#: an expired state.
REFUSED_LINKS: dict[str, Callable[[], str]] = {
    "garbage": lambda: "not-a-token",
    "forged": lambda: link_token(secret=OTHER_SECRET),
    "a_state_not_a_link": lambda: state_token(),
    "forged_and_expired": lambda: stale(lambda: link_token(secret=OTHER_SECRET)),
    "past_recognition": lambda: stale(link_token, age_s=31 * 24 * 3600),
}


class TestMounting:
    async def test_nothing_is_mounted_while_composio_is_off(
        self, settings: Settings, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        application = create_app(settings)
        application.state.db = FakeDatabase()
        async with make_client(application) as client:
            assert (await client.get("/connect/anything")).status_code == 404
            assert (await client.post("/connect/anything")).status_code == 404
            assert (await client.get("/connect/callback")).status_code == 404


class TestRefusedLinks:
    @pytest.mark.parametrize("kind", sorted(REFUSED_LINKS))
    async def test_the_page_is_refused_in_both_languages(
        self, http: AsyncClient, kind: str
    ) -> None:
        response = await http.get(f"/connect/{REFUSED_LINKS[kind]()}")
        assert response.status_code == 410
        assert_page(response, "link_unusable", "he", "en")
        assert_hardened(response)

    @pytest.mark.parametrize("kind", sorted(REFUSED_LINKS))
    async def test_the_button_is_refused_and_composio_never_called(
        self, http: AsyncClient, linker: FakeConnectLinker, kind: str
    ) -> None:
        response = await http.post(f"/connect/{REFUSED_LINKS[kind]()}")
        assert response.status_code == 410
        assert_hardened(response)
        assert linker.links == []

    async def test_the_refusal_does_not_say_why(self, http: AsyncClient) -> None:
        bodies = {(await http.get(f"/connect/{make()}")).text for make in REFUSED_LINKS.values()}
        assert len(bodies) == 1


def callback(state: str | None, account_id: str | None = ACCOUNT_ID) -> str:
    params = {
        name: value
        for name, value in {"state": state, "connected_account_id": account_id}.items()
        if value is not None
    }
    return f"/connect/callback?{urlencode(params)}"


REFUSED_STATES: dict[str, Callable[[], str | None]] = {
    "missing": lambda: None,
    "garbage": lambda: "not-a-state",
    "forged": lambda: state_token(secret=OTHER_SECRET),
    "a_link_not_a_state": lambda: link_token(),
    "forged_and_expired": lambda: stale(lambda: state_token(secret=OTHER_SECRET)),
    "past_recognition": lambda: stale(state_token, age_s=31 * 24 * 3600),
}


class TestCallbackRefusals:
    """D5, everything decided before the tenant's rows are touched."""

    @pytest.mark.parametrize("kind", sorted(REFUSED_STATES))
    async def test_a_bad_state_is_refused_before_composio_is_asked(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate, kind: str
    ) -> None:
        response = await http.get(callback(REFUSED_STATES[kind]()))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
        assert_hardened(response)
        assert linker.lookups == []
        assert queue.calls == []

    @pytest.mark.parametrize(
        "account_id", [None, "", "../../admin", "ca_" + "x" * 65, "ac_test", "ca_a/b"]
    )
    async def test_a_malformed_account_id_never_reaches_composio(
        self, http: AsyncClient, linker: FakeConnectLinker, account_id: str | None
    ) -> None:
        response = await http.get(callback(state_token(), account_id))
        assert response.status_code == 400
        assert linker.lookups == []

    async def test_another_users_account_is_refused(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, user_id=str(uuid4()))
        with capture_logs() as logs:
            response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
        assert linker.lookups == [ACCOUNT_ID]
        assert queue.calls == []
        # Refused after the state was verified: the log says whose callback it was.
        [refused] = [entry for entry in logs if entry["event"] == "connect.callback_refused"]
        assert refused == {
            "event": "connect.callback_refused",
            "log_level": "warning",
            "reason": "user_mismatch",
            "tenant_id": str(tenant_id),
        }

    async def test_a_refusal_before_verification_names_no_tenant(self, http: AsyncClient) -> None:
        with capture_logs() as logs:
            await http.get(callback(state_token(secret=OTHER_SECRET)))
        [refused] = [entry for entry in logs if entry["event"] == "connect.callback_refused"]
        assert "tenant_id" not in refused

    async def test_an_account_under_another_auth_config_is_refused(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, auth_config_id="ac_someone_else")
        response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert queue.calls == []

    @pytest.mark.parametrize("status", ["INITIALIZING", "INITIATED"])
    async def test_an_account_still_connecting_can_be_reloaded(
        self,
        http: AsyncClient,
        linker: FakeConnectLinker,
        queue: FakeProcrastinate,
        status: str,
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, status=status)
        response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert_page(response, "not_ready", "he", "en")
        assert queue.calls == []

    @pytest.mark.parametrize("status", ["FAILED", "EXPIRED", "INACTIVE", "REVOKED", "NEW_ONE"])
    async def test_an_account_that_will_not_become_active_asks_for_a_new_link(
        self,
        http: AsyncClient,
        linker: FakeConnectLinker,
        queue: FakeProcrastinate,
        status: str,
    ) -> None:
        """A reload cannot help these, so the page does not suggest one."""
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, status=status)
        response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
        assert page_text("not_ready", "en")["title"] not in response.text
        assert queue.calls == []

    async def test_an_account_composio_does_not_know_is_refused(self, http: AsyncClient) -> None:
        response = await http.get(callback(state_token()))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")

    async def test_composio_unreachable_asks_for_a_reload(
        self, http: AsyncClient, linker: FakeConnectLinker
    ) -> None:
        linker.failure = CalendarProviderUnavailableError("down")
        response = await http.get(callback(state_token()))
        assert response.status_code == 503
        assert_page(response, "unavailable_retry", "he", "en")
        assert_hardened(response)

    async def test_the_callback_is_not_mistaken_for_a_link(self, http: AsyncClient) -> None:
        """``/{token}`` would take "callback" as a token if it were declared first."""
        response = await http.get("/connect/callback")
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")


class TestUnexpectedFaults:
    """A fault after the checks still answers with a page and D4's headers, never Starlette's
    bare 500. Here the fault is the fake database, which has no ``tenant_session``."""

    async def test_the_page(self, http: AsyncClient) -> None:
        with sentry_events() as events, capture_logs() as logs:
            response = await http.get(f"/connect/{link_token()}")
        assert response.status_code == 500
        assert_page(response, "unavailable_retry", "he", "en")
        assert_hardened(response)
        assert [event["exception"]["values"][-1]["type"] for event in events] == ["AttributeError"]
        [failed] = [entry for entry in logs if entry["event"] == "connect.failed"]
        assert failed == {
            "event": "connect.failed",
            "log_level": "error",
            "error_type": "AttributeError",
        }

    async def test_the_button_asks_for_a_new_link(
        self, http: AsyncClient, linker: FakeConnectLinker
    ) -> None:
        """A POST may have spent the link before it failed, so a reload would not help."""
        with sentry_events() as events:
            response = await http.post(f"/connect/{link_token()}")
        assert response.status_code == 500
        assert_page(response, "unavailable_new_link", "he", "en")
        assert_hardened(response)
        assert len(events) == 1
        assert linker.links == []

    async def test_the_callback_asks_for_a_reload(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        token = state_token(tenant_id)
        with sentry_events() as events:
            response = await http.get(callback(token))
        assert response.status_code == 500
        assert_page(response, "unavailable_retry", "he", "en")
        assert_hardened(response)
        assert len(events) == 1
        assert token not in str(events)
        assert queue.calls == []
