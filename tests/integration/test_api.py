"""The HTTP surface, exercised for real.

These exist because of a specific failure mode that static checking cannot see:
FastAPI resolves route annotations when it builds each route, so a parameter
typed with a TYPE_CHECKING-only import passes mypy, passes ruff, and then returns
500 on the first request that hits it. That has now happened three times in this
codebase, on three different surfaces.

So every route is called at least once here. A route that cannot build its own
signature fails this suite instead of the demo.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from slidequote.api.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REQUEST = (
    "Ciao, mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici 16A, "
    "consegna a Rovereto"
)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    # The app reads Settings() from the environment when the lifespan starts, so
    # this has to be set before LifespanManager runs. 300x keeps the whole suite
    # in single-digit seconds while staying well under the ceiling where
    # execution overhead starts eating a supplier's SLA (see the integration
    # fixture in test_happy_path.py).
    monkeypatch.setenv("SLIDEQUOTE_DEMO__TIME_ACCELERATION", "300")
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http


async def _first_professional(client: httpx.AsyncClient) -> dict[str, Any]:
    state = (await client.get("/api/demo/state")).json()
    return state["professionals"][0]  # type: ignore[no-any-return]


async def _place_an_order(client: httpx.AsyncClient) -> dict[str, Any]:
    professional = await _first_professional(client)
    await client.post(
        "/api/simulator/message",
        json={"professional_id": professional["id"], "body": REQUEST},
    )
    order = await _wait_for_status(client, "awaiting_confirmation")
    await client.post(
        "/api/simulator/message", json={"professional_id": professional["id"], "body": "ok"}
    )
    return await _wait_for_status(client, "placed", fallback=order)


async def _wait_for_status(
    client: httpx.AsyncClient,
    status: str,
    *,
    attempts: int = 60,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Poll until the background canvassing reaches a status.

    The API answers before the work is done, exactly as a Twilio webhook must, so
    a test that asserts immediately would be asserting against a half-finished
    order rather than a bug.
    """
    for _ in range(attempts):
        orders = (await client.get("/api/orders")).json()
        for order in orders:
            if order["status"] == status:
                return order  # type: ignore[no-any-return]
        await asyncio.sleep(0.1)
    if fallback is not None:
        return fallback
    raise AssertionError(f"no order reached {status!r}")


class TestEveryRouteBuilds:
    """One call per route. A route that cannot resolve its own annotations 500s."""

    async def test_health(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["all_mocked"] is True

    async def test_demo_state(self, client: httpx.AsyncClient) -> None:
        state = (await client.get("/api/demo/state")).json()
        assert len(state["professionals"]) >= 3
        assert len(state["suppliers"]) >= 6
        assert state["time_acceleration"] > 0

    async def test_conversation_by_professional(self, client: httpx.AsyncClient) -> None:
        professional = await _first_professional(client)
        response = await client.get(f"/api/conversations/{professional['id']}")
        assert response.status_code == 200

    async def test_orders_list(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/orders")).status_code == 200

    async def test_order_detail_and_audit_and_artifact(
        self, client: httpx.AsyncClient
    ) -> None:
        """The three routes that take a path parameter — where the bug lived."""
        placed = await _place_an_order(client)

        detail = await client.get(f"/api/orders/{placed['id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["quotes"]

        audit = await client.get(f"/api/orders/{placed['id']}/audit")
        assert audit.status_code == 200
        assert audit.json()

        reference = body["quotes"][0]["artifact_reference"]
        artifact = await client.get(f"/api/artifacts/{reference}")
        assert artifact.status_code == 200, artifact.text
        assert artifact.json()["content"].strip()

    async def test_unknown_ids_are_404_not_500(self, client: httpx.AsyncClient) -> None:
        missing = "00000000-0000-0000-0000-000000000000"
        assert (await client.get(f"/api/orders/{missing}")).status_code == 404
        assert (await client.get("/api/artifacts/nope:nothing")).status_code == 404
        assert (
            await client.post(
                "/api/simulator/message", json={"professional_id": missing, "body": "ciao"}
            )
        ).status_code == 404


class TestTheFlowOverHttp:
    async def test_a_message_reaches_a_placed_order(self, client: httpx.AsyncClient) -> None:
        placed = await _place_an_order(client)
        assert placed["status"] == "placed"

        detail = (await client.get(f"/api/orders/{placed['id']}")).json()
        assert detail["winning_quote_id"] is not None
        bodies = [m["body"] for m in detail["messages"] if m["direction"] == "outbound"]
        assert any("Ordine confermato" in b for b in bodies)

    async def test_the_api_answers_before_the_work_is_done(
        self, client: httpx.AsyncClient
    ) -> None:
        """A webhook has about five seconds. Canvassing takes minutes.

        The POST must return the acknowledgement only; the quote arrives later.
        """
        professional = await _first_professional(client)
        response = await client.post(
            "/api/simulator/message",
            json={"professional_id": professional["id"], "body": REQUEST},
        )
        assert response.status_code == 200
        bodies = [m["body"] for m in response.json() if m["direction"] == "outbound"]
        assert any("Ricevuto" in b for b in bodies)
        assert not any("Miglior prezzo" in b for b in bodies)

    async def test_a_voice_transcription_takes_the_same_path(
        self, client: httpx.AsyncClient
    ) -> None:
        professional = await _first_professional(client)
        response = await client.post(
            "/api/simulator/message",
            json={"professional_id": professional["id"], "body": REQUEST, "channel": "voice"},
        )
        assert response.status_code == 200
        await _wait_for_status(client, "awaiting_confirmation")


class TestStream:
    async def test_the_event_stream_replays_and_then_follows(
        self, client: httpx.AsyncClient
    ) -> None:
        professional = await _first_professional(client)
        await client.post(
            "/api/simulator/message",
            json={"professional_id": professional["id"], "body": REQUEST},
        )
        await _wait_for_status(client, "awaiting_confirmation")

        # The replay buffer is what stops a browser opening mid-order from
        # showing an empty screen until the next thing happens.
        async with client.stream("GET", "/api/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            kinds: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    kinds.append(line.removeprefix("event: "))
                if len(kinds) >= 4:
                    break

        assert "message_received" in kinds
        assert any(k in kinds for k in ("supplier_answered", "quotes_ranked", "order_created"))
