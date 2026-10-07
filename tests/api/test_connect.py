"""``/connect`` without a database: everything refused before one is reached.

The fake database has no ``tenant_session``, so a request that reached it would answer 500
instead of the page asserted. That is how "refused before touching the database" is pinned.
The flow against Postgres is ``tests/db/test_connect.py``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from itsdangerous import TimestampSigner

from personal_organizer.api.app import create_app
from personal_organizer.settings import Settings
from tests.api.conftest import FakeDatabase, FakeProcrastinate
from tests.fixtures.connect import (
    CONNECT_ENV,
    OTHER_SECRET,
    FakeConnectLinker,
    assert_hardened,
    assert_page,
    connect_app,
    link_token,
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


def stale(make: Callable[[], str]) -> str:
    """A token signed two hours ago: past both the link's and the state's max age."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda _self: int(time.time()) - 7200)
        return make()


REFUSED_LINKS: dict[str, Callable[[], str]] = {
    "garbage": lambda: "not-a-token",
    "forged": lambda: link_token(secret=OTHER_SECRET),
    "a_state_not_a_link": lambda: state_token(),
    "expired": lambda: stale(link_token),
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
