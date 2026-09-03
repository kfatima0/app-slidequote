"""The HTTP surface for the demo harness and the future Ops Console.

Not the product. The product is a phone number; this exists so the experience can
be shown without Twilio, and so ops has somewhere to intervene (§3.1, §3.8).

Every route is a thin translation: read state, or hand a command to the
orchestrator. No decision is made here. `tests/test_architecture.py` keeps the
domain honest; this module's discipline is that it contains no `if` that changes
what the business does.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from yquo.api import mappers, twilio_voice, twilio_webhooks
from yquo.api.schemas import (
    ArtifactOut,
    AuditEntryOut,
    DemoStateOut,
    MessageOut,
    OrderDetailOut,
    OrderSummaryOut,
    SendMessageIn,
)
from yquo.api.sessions import SessionRegistry
from yquo.app.orchestrator import Orchestrator
from yquo.config.settings import Settings
from yquo.domain.enums import OrderStatus, ProfessionalChannel
from yquo.ports.messaging import InboundMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: Heartbeat so proxies and browsers keep an idle SSE connection open.
SSE_KEEPALIVE_S = 15.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    registry = SessionRegistry(settings=Settings())
    app.state.sessions = registry
    # Provider webhooks act on the shared world and cannot create one, so it is
    # built up front rather than on the first Twilio callback.
    await registry.get("shared")
    try:
        yield
    finally:
        await app.state.sessions.shutdown()


app = FastAPI(title="YQuo demo harness", version="0.1.0", lifespan=lifespan)

# Only needed in development, where the Astro dev server is a separate origin.
# A deployed build is served from this same app, so nothing is cross-origin and
# this middleware never applies. Demo-only regardless: there is nothing here to
# protect and no real credentials exist in this phase.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4321", "http://127.0.0.1:4321"],
    allow_methods=["*"],
    allow_headers=["*"],
)

#: The built frontend, when there is one. Mounted last so every /api route above
#: still wins; anything else falls through to the page.
#:
#: The default is the repo layout. A container lays things out differently, so
#: YQUO_FRONTEND_DIST overrides it rather than the image having to reproduce a
#: directory structure to satisfy a relative path.
FRONTEND_DIST = Path(
    os.environ.get(
        "YQUO_FRONTEND_DIST",
        str(Path(__file__).resolve().parents[3].parent / "frontend" / "dist"),
    )
)


#: The browser generates this and keeps it in local storage. Sent as a header on
#: ordinary requests and as a query parameter on the event stream, because
#: EventSource cannot set headers.
SESSION_HEADER = "X-YQuo-Session"


def session_id(request: Request) -> str:
    """Which visitor is this.

    Missing or malformed falls back to one shared world. That keeps curl and the
    /docs page usable without ceremony, while any real browser sends its own id
    and gets its own world.
    """
    registry: SessionRegistry = request.app.state.sessions
    candidate = request.headers.get(SESSION_HEADER) or request.query_params.get("session")
    if candidate and registry.is_valid(candidate):
        return candidate
    return "shared"


async def get_orchestrator(request: Request) -> Orchestrator:
    registry: SessionRegistry = request.app.state.sessions
    return await registry.get(session_id(request))


def get_orchestrator_sync(request: Request) -> Orchestrator:
    """The shared world, for callers that cannot await a dependency.

    Provider webhooks belong to no browser session: Twilio has no session id and
    a supplier replying to a real message is not a demo visitor. They act on the
    shared world, which is the same one a request without a session id gets.
    """
    registry: SessionRegistry = request.app.state.sessions
    existing = registry.peek("shared")
    if existing is None:
        raise HTTPException(status_code=503, detail="no shared world yet")
    return existing


async def get_orchestrator_for_socket(socket: WebSocket) -> Orchestrator:
    """The same shared world, reached from a websocket.

    A websocket has app state but is not a Request, so it cannot go through the
    dependency above. Provider sockets are not session-scoped for the same
    reason provider webhooks are not.

    Unlike the webhook accessor this creates the world if it is missing, rather
    than refusing. A refusal here is a live phone call with nobody on our end,
    and the shared world is the same one any request without a session id gets.
    """
    registry: SessionRegistry = socket.app.state.sessions
    return await registry.get("shared")


# Runtime imports above, not TYPE_CHECKING ones: FastAPI resolves these
# annotations when it builds each route, so a type-checker-only import leaves the
# route unbuildable and fails on the first request instead of at import time.
Orch = Annotated[Orchestrator, Depends(get_orchestrator)]


# Provider webhooks. Mounted separately because they are signed rather than
# session-scoped, and because they answer with TwiML rather than JSON.
app.include_router(twilio_webhooks.router)
app.include_router(twilio_voice.router)


@app.get("/api/health")
async def health(request: Request, orch: Orch) -> dict[str, object]:
    registry: SessionRegistry = request.app.state.sessions
    return {
        "status": "ok",
        "all_mocked": orch.ctx.settings.adapters.all_mocked,
        "now": orch.ctx.clock.now().isoformat(),
        "active_sessions": registry.active,
    }


@app.get("/api/demo/state", response_model=DemoStateOut)
async def demo_state(orch: Orch) -> DemoStateOut:
    ctx = orch.ctx
    return DemoStateOut(
        time_acceleration=ctx.clock.acceleration,
        now=ctx.clock.now(),
        all_mocked=ctx.settings.adapters.all_mocked,
        professionals=tuple(
            mappers.professional_out(p) for p in await ctx.repos.professionals.list_all()
        ),
        suppliers=tuple(
            mappers.supplier_out(s) for s in await ctx.repos.suppliers.list_all()
        ),
        open_orders=len(await ctx.repos.orders.list_open()),
    )


@app.post("/api/simulator/message", response_model=list[MessageOut])
async def send_message(payload: SendMessageIn, orch: Orch) -> list[MessageOut]:
    """A message typed into the simulator, or a call transcription.

    Returns immediately with the thread so far. Canvassing continues in the
    background and reaches the browser over SSE, exactly as a real webhook must
    answer before the work is done.
    """
    ctx = orch.ctx
    professional = await ctx.repos.professionals.get(payload.professional_id)
    if professional is None:
        raise HTTPException(status_code=404, detail="professional not found")

    channel = ProfessionalChannel(payload.channel)
    inbound = InboundMessage(
        from_phone_e164=professional.phone_e164,
        body=payload.body,
        channel=channel,
        external_message_id=f"SIM-{ctx.clock.now().timestamp():.6f}",
        received_at=ctx.clock.now(),
    )
    await orch.handle_inbound(inbound)

    messages = await ctx.repos.conversations.list_by_professional(professional.id)
    return [mappers.message_out(m) for m in messages]


@app.get("/api/conversations/{professional_id}", response_model=list[MessageOut])
async def conversation(professional_id: UUID, orch: Orch) -> list[MessageOut]:
    messages = await orch.ctx.repos.conversations.list_by_professional(professional_id)
    return [mappers.message_out(m) for m in messages]


@app.get("/api/orders", response_model=list[OrderSummaryOut])
async def list_orders(orch: Orch) -> list[OrderSummaryOut]:
    ctx = orch.ctx
    names = {p.id: p.business_name for p in await ctx.repos.professionals.list_all()}
    summaries = []
    for order in sorted(
        await ctx.repos.orders.list_by_status(list(OrderStatus)),
        key=lambda o: o.updated_at,
        reverse=True,
    ):
        quotes = await ctx.repos.quotes.list_by_order(order.id)
        summaries.append(
            mappers.order_summary_out(
                order, names.get(order.professional_id, "?"), quote_count=len(quotes)
            )
        )
    return summaries


@app.get("/api/orders/{order_id}", response_model=OrderDetailOut)
async def order_detail(order_id: UUID, orch: Orch) -> OrderDetailOut:
    ctx = orch.ctx
    order = await ctx.repos.orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    professional = await ctx.repos.professionals.get(order.professional_id)
    quotes = list(await ctx.repos.quotes.list_by_order(order.id))
    supplier_names = {s.id: s.name for s in await ctx.repos.suppliers.list_all()}
    messages = list(await ctx.repos.conversations.list_by_order(order.id))

    matched: dict[UUID, str] = {}
    for item in order.items:
        if item.matched_product_id is None:
            continue
        product = await ctx.repos.products.get(item.matched_product_id)
        if product is not None:
            matched[item.matched_product_id] = product.raw_description

    return mappers.order_detail_out(
        order,
        professional.business_name if professional else "?",
        sorted(quotes, key=lambda q: -(q.ranking_score or 0.0)),
        supplier_names,
        messages,
        matched,
    )


@app.get("/api/artifacts/{reference:path}", response_model=ArtifactOut)
async def artifact(reference: str, orch: Orch) -> ArtifactOut:
    """The evidence behind a quote: transcript, email thread, or basket."""
    found = await orch.ctx.repos.artifacts.get(reference)
    if found is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return mappers.artifact_out(found)


@app.get("/api/orders/{order_id}/audit", response_model=list[AuditEntryOut])
async def order_audit(order_id: UUID, orch: Orch) -> list[AuditEntryOut]:
    records = await orch.ctx.repos.audit.list_by_order(order_id)
    return [mappers.audit_out(r) for r in records]


@app.get("/api/stream")
async def stream(request: Request, orch: Orch) -> StreamingResponse:
    """Server-sent events: what is happening, as it happens.

    Display only. A browser that disconnects mid-canvass changes nothing about
    the order, which is what keeps the frontend disposable.
    """
    bus = orch.ctx.bus

    async def frames() -> AsyncIterator[str]:
        for past in bus.history():
            yield _frame(mappers.event_out(past).model_dump(mode="json"))

        subscription = bus.subscribe()
        while True:
            if await request.is_disconnected():
                break
            try:
                async with asyncio.timeout(SSE_KEEPALIVE_S):
                    event = await anext(subscription)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:  # pragma: no cover - bus never closes
                break
            yield _frame(mappers.event_out(event).model_dump(mode="json"))

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _frame(payload: dict[str, object]) -> str:
    return f"event: {payload['kind']}\ndata: {json.dumps(payload)}\n\n"


@app.post("/api/demo/reset", response_model=DemoStateOut)
async def reset(request: Request) -> DemoStateOut:
    """Throw the world away and rebuild it from the fixtures.

    Everything is in memory and every id is derived from a stable name, so a
    reset returns to exactly the same starting point. That is what makes a
    rehearsed walkthrough safe to repeat in front of an audience.

    Scoped to the caller: one visitor resetting cannot wipe another's demo.
    """
    registry: SessionRegistry = request.app.state.sessions
    fresh = await registry.reset(session_id(request))
    return await demo_state(fresh)


def mount_frontend(app: FastAPI, dist: Path = FRONTEND_DIST) -> bool:
    """Serve the built demo page from this app, if it has been built.

    Absent in development, where Astro serves it with hot reload. Present in a
    deployment, which is what turns two services into one URL.
    """
    if not (dist / "index.html").exists():
        return False
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return True


mount_frontend(app)
