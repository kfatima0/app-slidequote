"""The collection endpoint, and the two operator actions.

Behavioural rather than schema tests: every route is called for real, because
the failure this codebase keeps producing is a route that builds fine and dies
on its first request.

The assertions are mostly about honesty. A missing field must arrive with the
reason it is missing rather than as a blank; a corrected total must not erase
what the model read; an override must re-run the review flags rather than
leaving a warning that no longer applies.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from yquo.api.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    # A canvassing round takes minutes of simulated time. Accelerated hard so the
    # suite waits for the real orchestration rather than for a clock.
    monkeypatch.setenv("YQUO_DEMO__TIME_ACCELERATION", "600")
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http


async def an_order(client: httpx.AsyncClient) -> dict[str, Any]:
    """Drive a real request through intake and canvassing.

    The simulator endpoint answers as soon as the acknowledgement is sent and
    canvasses in the background, exactly as a webhook does. So this waits for the
    supplier lanes to exist rather than assuming they do: asserting on a screen
    the server has not finished drawing is how a test goes green by accident.
    """
    state = (await client.get("/api/demo/state")).json()
    professional = state["professionals"][0]
    await client.post(
        "/api/simulator/message",
        json={
            "professional_id": professional["id"],
            "body": "Mi servono 100 metri di cavo FROR 3x2.5, consegna a Rovereto",
        },
    )
    orders = (await client.get("/api/orders")).json()
    assert orders
    order = dict(orders[0])

    for _ in range(400):
        body = await collection(client, order["id"])
        # Lanes appear before anything is sent. Waiting for the first timeline
        # entry means waiting for the round to have actually done something.
        if any(lane["timeline"] for lane in body["suppliers"]):
            return order
        await asyncio.sleep(0.02)
    raise AssertionError("canvassing never produced any supplier activity")


async def collection(client: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/orders/{order_id}/collection")
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestCollectionView:
    async def test_every_shortlisted_supplier_has_a_lane(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await collection(client, order["id"])
        assert body["suppliers"]
        assert all(lane["supplier_name"] for lane in body["suppliers"])

    async def test_the_state_tally_covers_every_state(
        self, client: httpx.AsyncClient
    ) -> None:
        """A missing key would make the strip flicker as counts appear."""
        order = await an_order(client)
        body = await collection(client, order["id"])
        assert set(body["state_counts"]) >= {
            "to_contact",
            "awaiting_reply",
            "quote_extracted",
            "discarded",
            "error",
        }

    async def test_a_lane_carries_its_own_timeline(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await collection(client, order["id"])
        assert any(lane["timeline"] for lane in body["suppliers"])

    async def test_the_spend_ledger_is_reported_against_the_ceiling(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await collection(client, order["id"])
        assert Decimal(body["spend"]["ceiling_eur"]) > 0
        assert body["spend"]["exhausted"] is False

    async def test_an_order_with_nothing_collected_still_answers(
        self, client: httpx.AsyncClient
    ) -> None:
        """A console that 404s on a quiet order is a console that looks broken."""
        from uuid import uuid4

        body = await collection(client, str(uuid4()))
        assert body["suppliers"] == []
        assert body["outstanding"] == 0


class TestExtractedQuotes:
    async def test_values_travel_as_strings(self, client: httpx.AsyncClient) -> None:
        """A Decimal through JSON becomes 175.99999999999997 on somebody's screen."""
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        quote = body["comparison"]["quotes"][0]
        assert isinstance(quote["total_incl_vat"]["value"], str)

    async def test_a_missing_field_arrives_with_its_reason(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        vat = body["comparison"]["quotes"][0]["vat_rate_pct"]
        assert vat["value"] is None
        assert vat["missing_reason"]

    async def test_the_summary_comes_with_the_table(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        assert "preventiv" in body["comparison"]["summary_it"]


class TestOverride:
    async def test_a_correction_is_stored_at_full_confidence(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        quote_id = body["comparison"]["quotes"][0]["id"]

        response = await client.post(
            f"/api/quotes/{quote_id}/override",
            json={"total_incl_vat": "199,90", "note": "letto dal PDF"},
        )
        assert response.status_code == 200, response.text
        updated = next(
            q for q in response.json()["comparison"]["quotes"] if q["id"] == quote_id
        )
        assert updated["total_incl_vat"]["value"] == "199.90"
        assert updated["total_incl_vat"]["confidence"] == 1.0
        assert "operator" in (updated["total_incl_vat"]["evidence"] or "")

    async def test_an_italian_number_is_accepted(
        self, client: httpx.AsyncClient
    ) -> None:
        """An operator correcting an Italian quote types an Italian number."""
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        quote_id = body["comparison"]["quotes"][0]["id"]

        response = await client.post(
            f"/api/quotes/{quote_id}/override", json={"total_incl_vat": "1.234,50"}
        )
        updated = next(
            q for q in response.json()["comparison"]["quotes"] if q["id"] == quote_id
        )
        assert updated["total_incl_vat"]["value"] == "1234.50"

    async def test_the_original_reading_is_kept(
        self, client: httpx.AsyncClient
    ) -> None:
        """Erasing it would make the next argument about a price unresolvable."""
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        quote = body["comparison"]["quotes"][0]
        was = quote["total_incl_vat"]["value"]

        await client.post(
            f"/api/quotes/{quote['id']}/override", json={"total_incl_vat": "199,90"}
        )
        audit = (await client.get(f"/api/orders/{order['id']}/audit")).json()
        entry = next(e for e in audit if e["action"] == "quote_overridden")
        assert entry["output"]["was"]["total_incl_vat"] == was
        assert entry["actor"].startswith("ops")

    async def test_an_unknown_quote_is_a_404(self, client: httpx.AsyncClient) -> None:
        from uuid import uuid4

        response = await client.post(
            f"/api/quotes/{uuid4()}/override", json={"total_incl_vat": "10"}
        )
        assert response.status_code == 404

    async def test_nonsense_is_ignored_rather_than_stored(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await _with_a_quote(client, order)
        quote = body["comparison"]["quotes"][0]
        was = quote["total_incl_vat"]["value"]

        response = await client.post(
            f"/api/quotes/{quote['id']}/override", json={"total_incl_vat": "circa"}
        )
        updated = next(
            q for q in response.json()["comparison"]["quotes"] if q["id"] == quote["id"]
        )
        assert updated["total_incl_vat"]["value"] == was


class TestTakeover:
    async def test_it_closes_that_supplier_only(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await collection(client, order["id"])
        target = next(
            lane
            for lane in body["suppliers"]
            if lane["state"] not in {"discarded", "error", "quote_extracted"}
        )

        response = await client.post(
            f"/api/orders/{order['id']}/suppliers/{target['supplier_id']}/takeover",
            json={"reason": "il fornitore vuole parlare con qualcuno"},
        )
        assert response.status_code == 200, response.text

        lanes = {lane["supplier_id"]: lane for lane in response.json()["suppliers"]}
        assert lanes[target["supplier_id"]]["state"] == "discarded"
        assert "operator" in lanes[target["supplier_id"]]["failure_reason"]
        assert sum(
            1 for lane in lanes.values() if lane["state"] == "discarded"
        ) < len(lanes) or len(lanes) == 1

    async def test_it_is_recorded_against_the_operator(
        self, client: httpx.AsyncClient
    ) -> None:
        order = await an_order(client)
        body = await collection(client, order["id"])
        target = body["suppliers"][0]
        await client.post(
            f"/api/orders/{order['id']}/suppliers/{target['supplier_id']}/takeover",
            json={"reason": "gestisco io"},
        )
        audit = (await client.get(f"/api/orders/{order['id']}/audit")).json()
        assert any(e["action"] == "supplier_taken_over" for e in audit)

    async def test_a_supplier_not_on_this_order_is_a_404(
        self, client: httpx.AsyncClient
    ) -> None:
        from uuid import uuid4

        order = await an_order(client)
        response = await client.post(
            f"/api/orders/{order['id']}/suppliers/{uuid4()}/takeover",
            json={"reason": "boh"},
        )
        assert response.status_code == 404

    async def test_a_reason_is_required(self, client: httpx.AsyncClient) -> None:
        """An unexplained takeover is invisible to whoever picks it up."""
        order = await an_order(client)
        body = await collection(client, order["id"])
        response = await client.post(
            f"/api/orders/{order['id']}/suppliers/{body['suppliers'][0]['supplier_id']}/takeover",
            json={"reason": ""},
        )
        assert response.status_code == 422


async def _with_a_quote(
    client: httpx.AsyncClient, order: dict[str, Any]
) -> dict[str, Any]:
    """Put one extracted quote on the order, through the real reply path.

    Uses a supplier the round did not already finish with. By the time
    canvassing has settled, the shortlisted contacts are terminal, and a reply
    from a closed contact is deliberately ignored -- which is the behaviour under
    test elsewhere, not a way to set one up.
    """
    from uuid import UUID

    from yquo.api.main import app as live
    from yquo.app import outreach, supplier_reply
    from yquo.domain.enums import ExtractionSource

    orch = live.state.sessions.peek("shared")
    assert orch is not None
    ctx = orch.ctx

    order_entity = await ctx.repos.orders.get(UUID(order["id"]))
    assert order_entity is not None

    contacts = await ctx.repos.supplier_contacts.list_by_order(order_entity.id)
    taken = {c.supplier_id for c in contacts}
    spare = [s for s in await ctx.repos.suppliers.list_all() if s.id not in taken]
    supplier = spare[0] if spare else None
    if supplier is None:
        open_contact = next(
            (c for c in contacts if c.state.value not in
             {"quote_extracted", "discarded", "error"}),
            None,
        )
        assert open_contact is not None, "no supplier left to answer"
        supplier = await ctx.repos.suppliers.get(open_contact.supplier_id)
        assert supplier is not None
    else:
        await outreach.open_contacts(ctx, order_entity, [supplier])
        await outreach.mark_contacted(ctx, order_entity.id, supplier.id)

    quote = await supplier_reply.handle(
        ctx,
        supplier,
        order_entity,
        source=ExtractionSource.WHATSAPP_TEXT,
        source_reference="SMquote1",
        text="Per il cavo FROR le faccio 176,00 euro, consegna in 3 giorni",
    )
    assert quote is not None
    return await collection(client, order["id"])
