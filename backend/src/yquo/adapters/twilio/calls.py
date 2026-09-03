"""Live calls in flight, and the coroutines waiting on them.

A call is placed by one coroutine and finished by another: `place_call` dials,
then a webhook and a websocket -- separate HTTP requests, arriving whenever
Twilio feels like it -- decide how it ended. Something has to hold the thread
between those, and that is this.

Process-local on purpose, and not per-session like the demo worlds are. It tracks
open sockets and calls currently ringing, which belong to this process and cannot
be shared, restored, or isolated per visitor. Nothing durable lives here: the
CallSession row is the record, and this is only what is waiting on it.

Three rules keep it honest:

* **Reserve before dialling.** Twilio can fetch the answer URL before
  `calls.create` has returned a SID, so the pending call is registered under our
  own token first and the SID is bound to it afterwards.
* **First resolution wins.** The status callback and the relay socket both try
  to finish a call, in either order. Resolving twice would mean two answers to
  one question, so the second is dropped.
* **The result is a plain field; the future is optional.** Resolution happens
  wherever the webhook landed, which under a test client or a threaded server is
  not the loop that dialled. So the outcome is recorded on the object and the
  waiting future -- if there is one -- is woken through its own loop, rather than
  the outcome living only inside a future belonging to somebody else's loop.

There is no lock. Every operation here is a handful of dictionary statements with
no await between them, which under an event loop is already atomic, and a lock
created on one loop and taken on another is precisely the deadlock this module
would otherwise cause.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from yquo.domain.enums import CallOutcome
from yquo.ports.voice import CallResult

if TYPE_CHECKING:
    from datetime import datetime

    from yquo.ports.voice import CallBrief, CallRequest, CallTurn


@dataclass
class PendingCall:
    """One call between dial and outcome."""

    token: str
    request: CallRequest
    brief: CallBrief
    started_at: datetime
    history: list[CallTurn] = field(default_factory=list)
    provider_call_id: str | None = None
    #: Set when the disclosure was actually spoken. A recording kept without it
    #: is refused by the domain, which is the point.
    disclosure_played: bool = False
    #: How it ended, once something decides. The record; the future below is
    #: only the doorbell.
    result: CallResult | None = None
    future: asyncio.Future[CallResult] | None = None

    def transcript(self) -> str:
        return "\n".join(
            f"{'Assistente' if turn.role == 'agent' else 'Fornitore'}: {turn.text}"
            for turn in self.history
        )

    async def wait(self, timeout: float) -> CallResult:
        """Block until something resolves this call, or give up.

        The future is created here rather than at reserve time, so it belongs to
        the loop that actually waits on it.
        """
        if self.result is not None:
            return self.result
        self.future = asyncio.get_running_loop().create_future()
        return await asyncio.wait_for(self.future, timeout=timeout)


class CallRegistry:
    """Calls in flight, addressable by our token or Twilio's SID."""

    def __init__(self) -> None:
        self._by_token: dict[str, PendingCall] = {}
        self._token_for_sid: dict[str, str] = {}

    def reserve(self, request: CallRequest, brief: CallBrief, *, at: datetime) -> PendingCall:
        pending = PendingCall(
            token=request.request_id, request=request, brief=brief, started_at=at
        )
        self._by_token[pending.token] = pending
        return pending

    def bind(self, token: str, provider_call_id: str) -> None:
        pending = self._by_token.get(token)
        if pending is None:
            return
        pending.provider_call_id = provider_call_id
        self._token_for_sid[provider_call_id] = token

    def by_token(self, token: str) -> PendingCall | None:
        return self._by_token.get(token)

    def by_call_id(self, provider_call_id: str) -> PendingCall | None:
        token = self._token_for_sid.get(provider_call_id)
        return self._by_token.get(token) if token else None

    def resolve(self, pending: PendingCall, result: CallResult) -> bool:
        """Finish a call. False if something else finished it first."""
        if pending.result is not None:
            return False
        pending.result = result
        self._by_token.pop(pending.token, None)
        if pending.provider_call_id:
            self._token_for_sid.pop(pending.provider_call_id, None)

        future = pending.future
        if future is not None and not future.done():
            # Woken through its own loop: under a threaded server the webhook
            # that resolved this call is not running where the dial is waiting.
            future.get_loop().call_soon_threadsafe(_settle, future, result)
        return True

    def abandon(self, pending: PendingCall, outcome: CallOutcome) -> None:
        """Give up on a call nothing is going to resolve.

        Used when the dial itself failed or the hard timeout elapsed. Produces a
        result rather than an exception, because voicemail and a dead line are
        outcomes the caller has to handle either way.
        """
        self.resolve(
            pending,
            CallResult(
                provider_call_id=pending.provider_call_id or pending.token,
                outcome=outcome,
                transcript=pending.transcript() or None,
                disclosure_played=pending.disclosure_played,
            ),
        )


def _settle(future: asyncio.Future[CallResult], result: CallResult) -> None:
    if not future.done():
        future.set_result(result)


#: One per process. See the module docstring: this is socket state, not world
#: state, and the demo's per-visitor isolation does not apply to it.
REGISTRY = CallRegistry()
