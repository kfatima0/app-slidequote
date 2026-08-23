"""One demo world per visitor.

The whole point of deploying this is that beta testers can open a link
independently. Everything lives in memory, so without isolation the first tester
would see a stranger's chat and a stranger's orders and conclude the product is
broken. These tests are the reason the link can be sent at all.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from yquo.api.main import SESSION_HEADER, app
from yquo.api.sessions import SessionRegistry
from yquo.config.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REQUEST = "Ciao, mi servono 100 metri di cavo FROR 3x2.5, consegna a Rovereto"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("YQUO_DEMO__TIME_ACCELERATION", "300")
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http


def as_visitor(client: httpx.AsyncClient, who: str) -> dict[str, str]:
    return {SESSION_HEADER: who}


async def place_order(client: httpx.AsyncClient, who: str) -> dict[str, Any]:
    headers = as_visitor(client, who)
    state = (await client.get("/api/demo/state", headers=headers)).json()
    professional = state["professionals"][0]
    await client.post(
        "/api/simulator/message",
        json={"professional_id": professional["id"], "body": REQUEST},
        headers=headers,
    )
    for _ in range(60):
        orders = (await client.get("/api/orders", headers=headers)).json()
        if orders and orders[0]["status"] == "awaiting_confirmation":
            return orders[0]  # type: ignore[no-any-return]
        await asyncio.sleep(0.1)
    raise AssertionError(f"{who} never reached awaiting_confirmation")


class TestVisitorsAreIsolated:
    async def test_one_visitor_cannot_see_another_s_orders(
        self, client: httpx.AsyncClient
    ) -> None:
        await place_order(client, "alice-000000")

        bob = (await client.get("/api/orders", headers=as_visitor(client, "bob-0000000"))).json()
        assert bob == [], "a second visitor must start with an empty world"

    async def test_one_visitor_cannot_see_another_s_chat(
        self, client: httpx.AsyncClient
    ) -> None:
        await place_order(client, "alice-000000")

        headers = as_visitor(client, "bob-0000000")
        state = (await client.get("/api/demo/state", headers=headers)).json()
        professional = state["professionals"][0]
        thread = (
            await client.get(f"/api/conversations/{professional['id']}", headers=headers)
        ).json()
        assert thread == [], "the same professional must have a separate thread per visitor"

    async def test_both_visitors_can_work_at_the_same_time(
        self, client: httpx.AsyncClient
    ) -> None:
        """Ten testers poking at the link independently is the actual use."""
        alice, bob = await asyncio.gather(
            place_order(client, "alice-000000"), place_order(client, "bob-0000000")
        )
        assert alice["id"] != bob["id"]
        assert alice["quote_count"] >= 1
        assert bob["quote_count"] >= 1

    async def test_one_visitor_resetting_does_not_wipe_another(
        self, client: httpx.AsyncClient
    ) -> None:
        await place_order(client, "alice-000000")
        await place_order(client, "bob-0000000")

        await client.post("/api/demo/reset", headers=as_visitor(client, "alice-000000"))

        alice = (await client.get("/api/orders", headers=as_visitor(client, "alice-000000"))).json()
        bob = (await client.get("/api/orders", headers=as_visitor(client, "bob-0000000"))).json()
        assert alice == []
        assert len(bob) == 1, "a stranger's reset must not touch this visitor's demo"

    async def test_a_visitor_keeps_their_world_across_requests(
        self, client: httpx.AsyncClient
    ) -> None:
        placed = await place_order(client, "alice-000000")
        again = (await client.get("/api/orders", headers=as_visitor(client, "alice-000000"))).json()
        assert [o["id"] for o in again] == [placed["id"]]


class TestFallback:
    @pytest.mark.parametrize("bad", ["", "no", "x" * 200, "has spaces", "semi;colon"])
    async def test_a_malformed_id_falls_back_rather_than_failing(
        self, client: httpx.AsyncClient, bad: str
    ) -> None:
        """curl and the /docs page send no id; that must still work."""
        response = await client.get("/api/orders", headers={SESSION_HEADER: bad})
        assert response.status_code == 200

    async def test_no_id_at_all_still_works(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/demo/state")).status_code == 200

    async def test_the_stream_takes_the_id_from_the_query_string(
        self, client: httpx.AsyncClient
    ) -> None:
        """EventSource cannot set headers, so the id travels in the URL."""
        async with client.stream("GET", "/api/stream?session=alice-000000") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")


class TestRegistryHousekeeping:
    async def test_worlds_are_capped(self) -> None:
        """A crawler sending random ids must not be able to exhaust memory."""
        registry = SessionRegistry(settings=Settings(), max_sessions=5)
        try:
            for index in range(12):
                await registry.get(f"visitor-{index:04d}")
            assert registry.active == 5
        finally:
            await registry.shutdown()

    async def test_idle_worlds_are_forgotten(self) -> None:
        registry = SessionRegistry(settings=Settings(), idle_ttl_s=0)
        try:
            await registry.get("alice-000000")
            # Everything already seen is idle at a zero TTL, so the next visitor
            # sweeps it away.
            await registry.get("bob-0000000")
            assert registry.active == 1
        finally:
            await registry.shutdown()

    async def test_a_returning_visitor_gets_the_same_world(self) -> None:
        registry = SessionRegistry(settings=Settings())
        try:
            first = await registry.get("alice-000000")
            second = await registry.get("alice-000000")
            assert first is second
        finally:
            await registry.shutdown()

    async def test_reset_replaces_the_world_but_keeps_the_stream(self) -> None:
        """The browser watching is not being reset, only the data it is watching."""
        registry = SessionRegistry(settings=Settings())
        try:
            before = await registry.get("alice-000000")
            bus = before.ctx.bus
            after = await registry.reset("alice-000000")
            assert after is not before
            assert after.ctx.bus is bus
        finally:
            await registry.shutdown()
