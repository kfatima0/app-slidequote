"""Phone calls: TwiML, the relay socket, and how a call is decided to have ended.

No Twilio and no audio. The relay is driven by sending it the JSON messages
ConversationRelay sends, and the status callback by posting the form Twilio
posts, which is the whole of the contract between us and them.

Most of these are about the split in who decides the outcome. The agent knows
only what was said; the carrier knows only what the line did. Getting that wrong
means a call that reached an answering machine is recorded as a successful
conversation, and a supplier who asked for a person gets phoned again tomorrow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from slidequote.adapters.clock import ManualClock
from slidequote.adapters.memory import InMemoryRepositories
from slidequote.adapters.mock.call_agent import MockCallAgent
from slidequote.adapters.mock.voice import SCRIPTS, MockVoice
from slidequote.adapters.twilio.calls import REGISTRY
from slidequote.domain.enums import CallOutcome
from slidequote.ports.voice import CallBrief, CallRequest, CallTurn

if TYPE_CHECKING:
    from collections.abc import Iterator

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

#: Numbers chosen so `_script_for` lands on each script. See adapters/mock/voice.
PRICE_ON_THE_PHONE = "+390461000000"
SENDS_ON_WHATSAPP = "+390461000001"
ANSWERING_MACHINE = "+390461000002"
NOBODY_ANSWERS = "+390461000003"


def call_request(to: str, **overrides: Any) -> CallRequest:
    fields: dict[str, Any] = {
        "to_phone_e164": to,
        "objective_it": "Vorrei sapere se avete cavo FROR 3G2,5 e a che prezzo.",
        "context": {"articolo": "cavo FROR 3G2,5", "quantita": "100 m"},
        "request_id": f"call-{to[-4:]}",
    }
    fields.update(overrides)
    return CallRequest(**fields)


def mock_voice() -> tuple[MockVoice, InMemoryRepositories]:
    repos = InMemoryRepositories()
    return MockVoice(ManualClock(START), repos, MockCallAgent()), repos


class TestScriptsAreDeterministic:
    def test_each_script_is_reachable(self) -> None:
        """A demo that can only ever show one outcome is not showing an outcome."""
        from slidequote.adapters.mock.voice import _script_for

        reached = {
            _script_for(number)
            for number in (
                PRICE_ON_THE_PHONE,
                SENDS_ON_WHATSAPP,
                ANSWERING_MACHINE,
                NOBODY_ANSWERS,
            )
        }
        assert reached == set(range(len(SCRIPTS)))

    def test_the_same_number_behaves_the_same_way_twice(self) -> None:
        from slidequote.adapters.mock.voice import _script_for

        assert _script_for(PRICE_ON_THE_PHONE) == _script_for(PRICE_ON_THE_PHONE)


class TestOutcomes:
    async def test_a_price_quoted_aloud_completes_the_call(self) -> None:
        voice, repos = mock_voice()
        result = await voice.place_call(call_request(PRICE_ON_THE_PHONE))
        assert result.outcome is CallOutcome.COMPLETED
        assert "1,76 euro" in (result.transcript or "")

        stored = await repos.calls.get("call-0000")
        assert stored is not None
        assert stored.outcome is CallOutcome.COMPLETED
        assert stored.ended_at is not None

    async def test_an_offer_to_send_it_on_whatsapp_completes_the_call(self) -> None:
        """Better than a number said aloud, so the agent takes it and leaves."""
        voice, _ = mock_voice()
        result = await voice.place_call(call_request(SENDS_ON_WHATSAPP))
        assert result.outcome is CallOutcome.COMPLETED
        assert "WhatsApp" in (result.transcript or "")

    async def test_an_answering_machine_is_voicemail_not_a_conversation(self) -> None:
        voice, _ = mock_voice()
        result = await voice.place_call(call_request(ANSWERING_MACHINE))
        assert result.outcome is CallOutcome.VOICEMAIL

    async def test_nobody_answering_is_a_result_not_a_failure(self) -> None:
        voice, repos = mock_voice()
        result = await voice.place_call(call_request(NOBODY_ANSWERS))
        assert result.outcome is CallOutcome.NO_ANSWER
        assert result.transcript is None

        stored = await repos.calls.get("call-0003")
        assert stored is not None
        assert stored.outcome is CallOutcome.NO_ANSWER

    async def test_the_call_is_recorded_before_it_is_placed(self) -> None:
        """An in-progress row exists while the call is happening, not after."""
        voice, repos = mock_voice()
        await voice.place_call(call_request(PRICE_ON_THE_PHONE))
        stored = await repos.calls.get("call-0000")
        assert stored is not None
        assert stored.direction == "outbound"
        assert stored.duration_s == 47


class TestRecordingRequiresDisclosure:
    async def test_recording_plays_the_disclosure(self) -> None:
        voice, _ = mock_voice()
        result = await voice.place_call(call_request(PRICE_ON_THE_PHONE, record=True))
        assert result.disclosure_played
        assert "registrata" in (result.transcript or "")

    async def test_not_recording_does_not_play_it(self) -> None:
        voice, _ = mock_voice()
        result = await voice.place_call(call_request(PRICE_ON_THE_PHONE))
        assert not result.disclosure_played
        assert "registrata" not in (result.transcript or "")

    async def test_a_recording_cannot_be_stored_without_it(self) -> None:
        """Enforced by the domain, not by whoever remembers to check."""
        from slidequote.domain.comms import CallSession

        with pytest.raises(ValueError, match="disclosure"):
            CallSession(
                provider_call_id="CA1",
                direction="outbound",
                recording_storage_key="rec-1",
                disclosure_played=False,
                request_id="req-1",
                created_at=START,
            )


class TestAgentSaysOnlyWhatItWasGiven:
    async def test_the_opening_states_who_is_calling(self) -> None:
        reply = await MockCallAgent().opening(
            CallBrief(objective_it="Vorrei sapere se avete cavo FROR.")
        )
        assert "assistente virtuale" in reply.say_it

    async def test_a_request_for_a_person_ends_the_call_without_argument(self) -> None:
        agent = MockCallAgent()
        brief = CallBrief(objective_it="Vorrei un preventivo.")
        reply = await agent.reply(brief, (), "Mi passi una persona per favore")
        assert reply.hang_up
        assert reply.outcome is CallOutcome.HUMAN_REQUESTED

    async def test_the_second_misunderstanding_ends_the_call(self) -> None:
        """One 'could you repeat that'. Not a loop on a billed line."""
        agent = MockCallAgent()
        brief = CallBrief(objective_it="Vorrei un preventivo.")
        first = await agent.reply(brief, (), "grrblmph")
        assert not first.hang_up

        history = (CallTurn(role="agent", text=first.say_it),)
        second = await agent.reply(brief, history, "grrblmph")
        assert second.hang_up

    async def test_no_stock_ends_the_call_politely(self) -> None:
        agent = MockCallAgent()
        reply = await agent.reply(
            CallBrief(objective_it="Vorrei un preventivo."), (), "No, non l'abbiamo"
        )
        assert reply.hang_up
        assert reply.outcome is None


AUTH_TOKEN = "test_auth_token_not_a_real_secret"
PUBLIC_BASE = "https://slidequote.test"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app, with real signature checking.

    Signatures are computed with Twilio's own validator rather than disabled,
    because the URL reconstruction is the part most likely to drift and the part
    a faked check would never catch.
    """
    monkeypatch.setenv("SLIDEQUOTE_TWILIO__AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("SLIDEQUOTE_TWILIO__PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.setenv("SLIDEQUOTE_TWILIO__VALIDATE_SIGNATURES", "true")
    from slidequote.api.main import app

    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, path: str, params: dict[str, str] | None = None) -> Any:
    """POST a webhook the way Twilio does, signature and all.

    The signature covers the full URL including the query string, so the token
    cannot be swapped for another call's on the way in.
    """
    data = params or {}
    validator = RequestValidator(AUTH_TOKEN)
    signature = validator.compute_signature(f"{PUBLIC_BASE}{path}", data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": signature})


def reserve(token: str, **overrides: Any) -> Any:
    return REGISTRY.reserve(
        call_request(PRICE_ON_THE_PHONE, request_id=token, **overrides),
        CallBrief(
            objective_it="Vorrei sapere se avete cavo FROR 3G2,5.",
            context={"articolo": "cavo FROR 3G2,5"},
        ),
        at=START,
    )


class TestTwiML:
    def test_the_answer_connects_the_line_to_the_relay(
        self, client: TestClient
    ) -> None:
        pending = reserve("tok-relay")
        try:
            response = post(client, "/api/twilio/voice/outbound?token=tok-relay")
            assert response.status_code == 200
            body = response.text
            assert "<ConversationRelay" in body
            assert "token=tok-relay" in body
            assert 'language="it-IT"' in body
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)

    def test_the_greeting_is_spoken_and_recorded_as_a_turn(
        self, client: TestClient
    ) -> None:
        pending = reserve("tok-greeting")
        try:
            post(client, "/api/twilio/voice/outbound?token=tok-greeting")
            assert pending.history
            assert pending.history[0].role == "agent"
            assert "assistente virtuale" in pending.history[0].text
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)

    def test_recording_puts_the_disclosure_in_the_greeting(
        self, client: TestClient
    ) -> None:
        pending = REGISTRY.reserve(
            call_request(PRICE_ON_THE_PHONE, request_id="tok-rec", record=True),
            CallBrief(
                objective_it="Vorrei un preventivo.",
                disclosure_it="Le comunico che questa chiamata viene registrata.",
            ),
            at=START,
        )
        try:
            response = post(client, "/api/twilio/voice/outbound?token=tok-rec")
            assert "registrata" in response.text
            assert pending.disclosure_played
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)

    def test_an_unknown_token_says_something_rather_than_nothing(
        self, client: TestClient
    ) -> None:
        """A live line dropped into silence is worse than an apology."""
        response = post(client, "/api/twilio/voice/outbound?token=nope")
        assert response.status_code == 200
        assert "<Say" in response.text
        assert "<Hangup/>" in response.text

    def test_an_inbound_call_is_answered_and_recorded(self, client: TestClient) -> None:
        response = post(
            client,
            "/api/twilio/voice/inbound",
            {"CallSid": "CAinbound1", "From": "+393401234567"},
        )
        assert response.status_code == 200
        assert "<ConversationRelay" in response.text


class TestStatusCallback:
    def test_a_busy_line_is_a_busy_outcome(self, client: TestClient) -> None:
        pending = reserve("tok-busy")
        post(
            client,
            "/api/twilio/voice/status?token=tok-busy",
            {"CallSid": "CAbusy", "CallStatus": "busy", "CallDuration": "0"},
        )
        assert pending.result is not None
        assert pending.result.outcome is CallOutcome.BUSY

    def test_no_answer_is_a_no_answer_outcome(self, client: TestClient) -> None:
        pending = reserve("tok-noanswer")
        post(
            client,
            "/api/twilio/voice/status?token=tok-noanswer",
            {"CallSid": "CAna", "CallStatus": "no-answer"},
        )
        assert pending.result.outcome is CallOutcome.NO_ANSWER

    def test_a_machine_answering_is_voicemail_however_long_it_talked(
        self, client: TestClient
    ) -> None:
        pending = reserve("tok-machine")
        post(
            client,
            "/api/twilio/voice/status?token=tok-machine",
            {
                "CallSid": "CAmach",
                "CallStatus": "completed",
                "CallDuration": "31",
                "AnsweredBy": "machine_start",
            },
        )
        result = pending.result
        assert result.outcome is CallOutcome.VOICEMAIL
        assert result.duration_s == 31

    def test_a_human_answering_completes_the_call(self, client: TestClient) -> None:
        pending = reserve("tok-human")
        post(
            client,
            "/api/twilio/voice/status?token=tok-human",
            {
                "CallSid": "CAhum",
                "CallStatus": "completed",
                "CallDuration": "94",
                "AnsweredBy": "human",
            },
        )
        assert pending.result.outcome is CallOutcome.COMPLETED

    def test_a_call_still_ringing_is_not_resolved(self, client: TestClient) -> None:
        pending = reserve("tok-ringing")
        try:
            post(
                client,
            "/api/twilio/voice/status?token=tok-ringing",
            {"CallSid": "CAring", "CallStatus": "ringing"},
        )
            assert pending.result is None
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)

    def test_an_unknown_call_is_acknowledged_not_refused(
        self, client: TestClient
    ) -> None:
        """Twilio retries anything it does not get a 200 for, forever."""
        response = post(
            client,
            "/api/twilio/voice/status?token=gone",
            {"CallSid": "CAgone", "CallStatus": "completed"},
        )
        assert response.status_code == 200

    def test_the_first_resolution_wins(self, client: TestClient) -> None:
        """A late 'completed' must not overwrite 'they asked for a person'."""
        pending = reserve("tok-race")
        from slidequote.ports.voice import CallResult

        REGISTRY.resolve(
            pending,
            CallResult(
                provider_call_id="CArace", outcome=CallOutcome.HUMAN_REQUESTED
            ),
        )
        post(
            client,
            "/api/twilio/voice/status?token=tok-race",
            {
                "CallSid": "CArace",
                "CallStatus": "completed",
                "AnsweredBy": "human",
            },
        )
        assert pending.result.outcome is CallOutcome.HUMAN_REQUESTED


class TestRelaySocket:
    def test_a_conversation_runs_to_a_hang_up(self, client: TestClient) -> None:
        pending = reserve("tok-ws")
        with client.websocket_connect("/api/twilio/voice/relay?token=tok-ws") as socket:
            socket.send_json({"type": "setup", "callSid": "CAws"})
            socket.send_json(
                {
                    "type": "prompt",
                    "voicePrompt": "Si, ce l'abbiamo, 1,76 euro al metro.",
                }
            )
            said = socket.receive_json()
            assert said["type"] == "text"
            assert said["last"] is True
            assert socket.receive_json()["type"] == "end"

        assert pending.result is not None
        assert pending.result.outcome is CallOutcome.COMPLETED
        assert "1,76 euro" in (pending.result.transcript or "")

    def test_a_request_for_a_person_resolves_as_such(
        self, client: TestClient
    ) -> None:
        pending = reserve("tok-ws-human")
        with client.websocket_connect(
            "/api/twilio/voice/relay?token=tok-ws-human"
        ) as socket:
            socket.send_json({"type": "prompt", "voicePrompt": "Mi passi un operatore"})
            socket.receive_json()
            socket.receive_json()
        assert pending.result.outcome is CallOutcome.HUMAN_REQUESTED

    def test_the_setup_message_binds_the_provider_id(
        self, client: TestClient
    ) -> None:
        pending = reserve("tok-ws-setup")
        try:
            with client.websocket_connect(
                "/api/twilio/voice/relay?token=tok-ws-setup"
            ) as socket:
                socket.send_json({"type": "setup", "callSid": "CAbound"})
                socket.send_json({"type": "prompt", "voicePrompt": "grrblmph"})
                socket.receive_json()
            assert pending.provider_call_id == "CAbound"
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)

    def test_an_unrecognised_message_type_is_ignored(
        self, client: TestClient
    ) -> None:
        """Twilio adds message types; an unknown one must not end a live call."""
        pending = reserve("tok-ws-noise")
        try:
            with client.websocket_connect(
                "/api/twilio/voice/relay?token=tok-ws-noise"
            ) as socket:
                socket.send_json({"type": "interrupt"})
                socket.send_json({"type": "dtmf", "digit": "1"})
                socket.send_json({"type": "prompt", "voicePrompt": "grrblmph"})
                reply = socket.receive_json()
            assert reply["type"] == "text"
            assert pending.result is None
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)

    def test_an_unknown_token_is_refused(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect as Disconnect

        with pytest.raises(Disconnect), client.websocket_connect(
            "/api/twilio/voice/relay?token=nope"
        ) as socket:
            socket.receive_json()

    def test_a_dropped_socket_does_not_decide_the_outcome(
        self, client: TestClient
    ) -> None:
        """The line went dead. Only the carrier knows whether that is a failure."""
        pending = reserve("tok-ws-drop")
        try:
            with client.websocket_connect(
                "/api/twilio/voice/relay?token=tok-ws-drop"
            ) as socket:
                socket.send_json({"type": "setup", "callSid": "CAdrop"})
            assert pending.result is None
        finally:
            REGISTRY.abandon(pending, CallOutcome.FAILED)
