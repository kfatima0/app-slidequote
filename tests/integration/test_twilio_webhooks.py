"""Twilio webhooks, driven with realistic payloads and real signatures.

Signatures are computed with Twilio's own validator rather than stubbed, so
these tests fail if the URL reconstruction drifts from what Twilio signs. That
is the failure most likely to happen in production and least likely to be caught
by a test that fakes the check.

Nothing here reaches the network. The Twilio SDK is used only for its signature
maths, which is pure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from twilio.request_validator import RequestValidator

from yquo.api.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

AUTH_TOKEN = "test_auth_token_not_a_real_secret"
PUBLIC_BASE = "https://yquo.test"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    # Real credentials are absent, so the messaging adapter stays mocked; only
    # the webhook signature path is exercised for real.
    monkeypatch.setenv("YQUO_TWILIO__AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("YQUO_TWILIO__PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.setenv("YQUO_TWILIO__VALIDATE_SIGNATURES", "true")
    monkeypatch.setenv("YQUO_DEMO__TIME_ACCELERATION", "300")
    async with LifespanManager(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http


def signed(path: str, params: dict[str, str]) -> dict[str, str]:
    """The header Twilio would send for this exact request."""
    validator = RequestValidator(AUTH_TOKEN)
    signature = validator.compute_signature(f"{PUBLIC_BASE}{path}", params)
    return {"X-Twilio-Signature": signature}


async def professional_number(client: httpx.AsyncClient) -> str:
    state = (await client.get("/api/demo/state")).json()
    return str(state["professionals"][0]["phone_e164"])


def inbound_payload(**overrides: Any) -> dict[str, str]:
    """A WhatsApp inbound as Twilio actually posts it."""
    payload = {
        "SmsMessageSid": "SM11111111111111111111111111111111",
        "MessageSid": "SM11111111111111111111111111111111",
        "AccountSid": "AC00000000000000000000000000000000",
        "MessagingServiceSid": "MG00000000000000000000000000000000",
        "From": "whatsapp:+393310000101",
        "To": "whatsapp:+14155238886",
        "Body": "Ciao, mi servono 100 metri di cavo FROR 3x2.5",
        "NumMedia": "0",
        "NumSegments": "1",
        "ProfileName": "Bortolotti Impianti",
        "WaId": "393310000101",
        "SmsStatus": "received",
        "ApiVersion": "2010-04-01",
    }
    payload.update({k: str(v) for k, v in overrides.items()})
    return payload


class TestSignatureIsEnforced:
    async def test_an_unsigned_request_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/twilio/inbound", data=inbound_payload())
        assert response.status_code == 403

    async def test_a_wrong_signature_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/twilio/inbound",
            data=inbound_payload(),
            headers={"X-Twilio-Signature": "obviously-not-right"},
        )
        assert response.status_code == 403

    async def test_a_signature_over_different_params_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """Tampering with one field after signing must invalidate the whole request."""
        payload = inbound_payload()
        headers = signed("/api/twilio/inbound", payload)
        payload["Body"] = "mi servono 500 metri di cavo"
        response = await client.post("/api/twilio/inbound", data=payload, headers=headers)
        assert response.status_code == 403

    async def test_a_correctly_signed_request_is_accepted(
        self, client: httpx.AsyncClient
    ) -> None:
        payload = inbound_payload()
        response = await client.post(
            "/api/twilio/inbound", data=payload, headers=signed("/api/twilio/inbound", payload)
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        assert "<Response>" in response.text


class TestIdempotency:
    async def test_the_same_message_twice_creates_one_order(
        self, client: httpx.AsyncClient
    ) -> None:
        """Twilio redelivers. A second delivery must not open a second order."""
        payload = inbound_payload(From=f"whatsapp:{await professional_number(client)}")
        headers = signed("/api/twilio/inbound", payload)

        first = await client.post("/api/twilio/inbound", data=payload, headers=headers)
        second = await client.post("/api/twilio/inbound", data=payload, headers=headers)
        assert first.status_code == second.status_code == 200

        orders = (await client.get("/api/orders")).json()
        assert len(orders) <= 1, "a redelivered webhook opened a second order"

    async def test_a_late_status_cannot_undo_a_newer_one(
        self, client: httpx.AsyncClient
    ) -> None:
        """Status callbacks arrive out of order; delivered must survive a late sent."""
        sid = "SM99999999999999999999999999999999"
        for status in ("delivered", "sent"):
            payload = {
                "MessageSid": sid,
                "MessageStatus": status,
                "AccountSid": "AC00000000000000000000000000000000",
                "From": "whatsapp:+14155238886",
                "To": "whatsapp:+393310000101",
            }
            response = await client.post(
                "/api/twilio/status", data=payload, headers=signed("/api/twilio/status", payload)
            )
            assert response.status_code == 200

        # Reaching into the shared world: there is no read API for provider
        # messages, and asserting the merge is the point of the test.
        registry = app.state.sessions
        orch = registry.peek("shared")
        assert orch is not None
        stored = await orch.ctx.repos.provider_messages.get(sid)
        assert stored is not None
        assert stored.status.value == "delivered"


class TestOptOut:
    @pytest.mark.parametrize("word", ["STOP", "stop", "Basta", "ALT", "cancella"])
    async def test_opt_out_words_revoke_consent(
        self, client: httpx.AsyncClient, word: str
    ) -> None:
        """The Italian words matter: Meta only honours the English ones itself."""
        number = await professional_number(client)
        payload = inbound_payload(
            MessageSid=f"SM{word.lower():0<32}"[:35],
            SmsMessageSid=f"SM{word.lower():0<32}"[:35],
            From=f"whatsapp:{number}",
            Body=word,
        )
        response = await client.post(
            "/api/twilio/inbound", data=payload, headers=signed("/api/twilio/inbound", payload)
        )
        assert response.status_code == 200

        orch = app.state.sessions.peek("shared")
        assert orch is not None
        assert await orch.ctx.repos.consent.may_contact(number) is False

    async def test_opting_back_in_restores_contact(self, client: httpx.AsyncClient) -> None:
        number = await professional_number(client)
        for index, word in enumerate(["STOP", "START"]):
            payload = inbound_payload(
                MessageSid=f"SM{index:0>32}",
                SmsMessageSid=f"SM{index:0>32}",
                From=f"whatsapp:{number}",
                Body=word,
            )
            await client.post(
                "/api/twilio/inbound", data=payload, headers=signed("/api/twilio/inbound", payload)
            )

        orch = app.state.sessions.peek("shared")
        assert orch is not None
        assert await orch.ctx.repos.consent.may_contact(number) is True

    async def test_an_opt_out_does_not_become_an_order(
        self, client: httpx.AsyncClient
    ) -> None:
        """STOP is a command, not a materials request."""
        number = await professional_number(client)
        payload = inbound_payload(
            MessageSid="SM55555555555555555555555555555555",
            SmsMessageSid="SM55555555555555555555555555555555",
            From=f"whatsapp:{number}",
            Body="STOP",
        )
        await client.post(
            "/api/twilio/inbound", data=payload, headers=signed("/api/twilio/inbound", payload)
        )
        assert (await client.get("/api/orders")).json() == []


class TestMedia:
    async def test_an_attachment_is_downloaded_and_kept(
        self, client: httpx.AsyncClient
    ) -> None:
        """Provider media URLs expire, so the bytes are pulled on receipt."""
        number = await professional_number(client)
        payload = inbound_payload(
            MessageSid="SM77777777777777777777777777777777",
            SmsMessageSid="SM77777777777777777777777777777777",
            From=f"whatsapp:{number}",
            Body="Ecco il preventivo",
            NumMedia="1",
            MediaUrl0="https://api.twilio.com/2010-04-01/Accounts/AC0/Messages/MM0/Media/ME0.pdf",
            MediaContentType0="application/pdf",
        )
        response = await client.post(
            "/api/twilio/inbound", data=payload, headers=signed("/api/twilio/inbound", payload)
        )
        assert response.status_code == 200

        orch = app.state.sessions.peek("shared")
        assert orch is not None
        stored = await orch.ctx.repos.media.get("SM77777777777777777777777777777777-0")
        assert stored is not None
        meta, content = stored
        assert meta.content_type == "application/pdf"
        assert content.startswith(b"%PDF"), "the bytes should be the file, not a link to it"
        assert meta.byte_size == len(content)


class TestUnknownSenders:
    async def test_a_message_from_an_unknown_number_is_acknowledged(
        self, client: httpx.AsyncClient
    ) -> None:
        """Twilio must get its 200 even when we do not know who sent it.

        Anything else and it retries the same unknown number every few minutes.
        """
        payload = inbound_payload(
            MessageSid="SM88888888888888888888888888888888",
            SmsMessageSid="SM88888888888888888888888888888888",
            From="whatsapp:+390000000000",
        )
        response = await client.post(
            "/api/twilio/inbound", data=payload, headers=signed("/api/twilio/inbound", payload)
        )
        assert response.status_code == 200
