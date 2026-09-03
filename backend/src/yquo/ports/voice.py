"""Phone calls, in and out.

Split from MessagingPort because a call is not a message with audio. It has a
duration, it can be answered by a machine, it can be abandoned halfway, and it
produces a recording that carries consent obligations a text message does not.
Modelling both through one interface would mean every caller handling outcomes
that cannot happen on their channel.

The real implementation is Twilio; the mock is the scripted transcript agent that
already exists. Both satisfy this, so orchestration never learns which it has.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from yquo.domain.enums import CallOutcome
from yquo.domain.values import Money, ValueObject


class CallRequest(ValueObject):
    """Ask for a call to be placed.

    `objective` is what the agent is trying to achieve, in Italian, and becomes
    part of the system prompt. `context` carries the structured facts the agent
    may state -- it is not free to invent quantities or delivery towns, so they
    are passed rather than described.
    """

    to_phone_e164: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    #: Absent on an inbound call, which belongs to neither yet: the caller has
    #: not said what they want, and inventing an order to hang it on would put a
    #: row in the ledger for a conversation that may go nowhere.
    order_id: UUID | None = None
    supplier_id: UUID | None = None
    objective_it: str = Field(min_length=1)
    context: dict[str, str] = Field(default_factory=dict)
    #: Hard cap. A call that runs longer is ended by us, not by the meter,
    #: because an agent stuck in a loop with an IVR bills by the minute.
    max_duration_s: int = Field(default=300, gt=0)
    #: Recording is opt-in per call and requires the spoken disclosure. Off by
    #: default so a caller has to decide, rather than inheriting it.
    record: bool = False
    request_id: str = Field(min_length=1)


class CallResult(ValueObject):
    """How a call ended, and what came out of it."""

    provider_call_id: str
    outcome: CallOutcome
    duration_s: int = Field(default=0, ge=0)
    transcript: str | None = None
    recording_storage_key: str | None = None
    disclosure_played: bool = False
    price: Money | None = None
    currency: str | None = None
    #: Free text from the agent about what happened, in Italian, for the Ops
    #: Console. Not parsed by anything.
    notes_it: str | None = None


class InboundCall(ValueObject):
    """A call arriving at our number."""

    provider_call_id: str
    from_phone_e164: str
    to_phone_e164: str
    received_at: datetime


class VoicePort(Protocol):
    async def place_call(self, request: CallRequest) -> CallResult:
        """Dial, hold the conversation, and return what happened.

        Never raises for an ordinary bad outcome. Voicemail, no answer and a busy
        line are results, not failures, and each drives a different next step.
        """
        ...

    async def answer_url(self) -> str:
        """The webhook Twilio fetches TwiML from when a call connects."""
        ...

    def supports_realtime(self) -> Literal[True, False]:
        """Whether this provider can hold a live conversation.

        False means the fallback path: play a structured prompt, record the
        reply, transcribe it afterwards. Callers use this to decide whether a
        supplier can be asked a follow-up question mid-call.
        """
        ...


class CallBrief(ValueObject):
    """What the agent on a call is allowed to know and say.

    Facts are passed rather than described. An agent told "ask about the cable"
    will improvise a quantity; an agent given `context={"quantita": "100 m"}` has
    nothing to improvise from. Everything the agent may state out loud is in
    here, and anything not in here it does not know.
    """

    order_id: UUID | None = None
    supplier_id: UUID | None = None
    supplier_name: str | None = None
    objective_it: str = Field(min_length=1)
    context: dict[str, str] = Field(default_factory=dict)
    #: Set when the call is being recorded, so the agent opens with the
    #: disclosure instead of the greeting. Not optional and not paraphrasable.
    disclosure_it: str | None = None


class CallTurn(ValueObject):
    """One thing said, by one side."""

    role: Literal["agent", "supplier"]
    text: str


class AgentReply(ValueObject):
    """What to say next, and whether that is the end of it.

    `outcome` is set only when the agent knows how the call ended -- the supplier
    asked for a person, or a machine picked up. Left None otherwise, so the
    ordinary case is decided by the provider's own status rather than guessed at
    from the words.
    """

    say_it: str
    hang_up: bool = False
    outcome: CallOutcome | None = None


class CallAgentPort(Protocol):
    """The brain on a call. Stateless: the history is passed in each turn.

    Stateless because the transport is. A relay delivers one utterance at a time
    over a socket that can drop and reconnect, and an agent holding its own
    memory would lose the thread exactly when the line got bad.
    """

    async def opening(self, brief: CallBrief) -> AgentReply:
        """The first thing said after the line connects."""
        ...

    async def reply(
        self, brief: CallBrief, history: tuple[CallTurn, ...], heard: str
    ) -> AgentReply:
        """What to say in response to what the supplier just said.

        Must not invent quantities, prices or dates. Anything it states about the
        order comes from `brief.context`; anything else it asks about rather than
        asserts.
        """
        ...
