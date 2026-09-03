# Decisions

Every choice made while replacing the mocks with Twilio and OpenAI, why it was
made, and what it costs. Written so the next person can disagree with a decision
without first having to reconstruct it.

Dates are when the vendor documentation was consulted. Model names, API shapes
and pricing move faster than this file will.

---

## 1. Both providers sit behind ports that already existed

The mock-first build put every edge behind a Protocol in `ports/`, with
`composition.py` as the only module naming a concrete adapter. That held. Adding
Twilio and OpenAI added three new ports and four new adapters; it changed nothing
under `domain/`, and under `app/` only in the sense that new orchestration was
written, not that existing orchestration was rewritten.

`tests/test_architecture.py` enforces the other half: nothing under `domain/` or
`app/` may import `adapters/`. That test is why the swap could not leak.

**Cost:** two implementations of everything, and the mock ones are real code that
has to keep working. The offline suite is the reason that cost is worth paying,
509 tests run with no key, no network, and no account anywhere.

## 2. Twilio ConversationRelay rather than raw Media Streams

*(Twilio Voice docs, consulted 2026-09-03.)*

ConversationRelay does speech recognition and speech synthesis on Twilio's side
and hands us text over a websocket. Media Streams hands us raw audio frames.

Taking the audio would mean owning a streaming transcription pipeline, a TTS
pipeline, two codecs, barge-in handling and turn detection, none of which is
this product's problem. The product's problem is knowing what to say.

**Cost:** the voice is Twilio's, the latency is Twilio's, and switching provider
later means rewriting the transport. Accepted: the agent that decides what to say
is behind `CallAgentPort` and is unaffected by that transport.

## 3. `place_call` waits on a future that a webhook resolves

`VoicePort.place_call` returns how the call ended. The ending is decided by two
other HTTP requests, the relay socket and the status callback, arriving later
and in either order.

So a dial registers a pending call in a process-local registry, then waits.
Whichever of those two finishes it first wins; the second is dropped.

Three consequences worth naming:

- **Reserve before dialling.** Twilio can fetch the answer URL before
  `calls.create` returns a SID, so the pending call is registered under our own
  token first and the SID is bound afterwards.
- **`CallSession` is keyed on our token, not Twilio's SID**, for the same reason.
  The SID goes in `provider_ref` once known.
- **The registry holds no lock and creates its future lazily**, on the loop that
  actually waits on it. An `asyncio.Lock` created on one loop and taken on
  another deadlocks, which is exactly what happened the first time this was
  written and is why the module says so at the top.

**Cost:** the registry is process-local, so a multi-process deployment needs the
webhook to reach the process that dialled. Sticky routing or a shared store; not
solved here, and named in RISKS.

## 4. Outcome resolution is split between the agent and the carrier

The agent knows only what was said. The carrier knows only what the line did.

- **From the conversation:** `human_requested`, `voicemail`.
- **From the status callback:** `busy`, `no_answer`, `failed`, `completed`, the
  duration, and `voicemail` again via answering-machine detection.

Letting the agent report `completed` would mean a call that reached an answering
machine gets recorded as a successful conversation. Letting the carrier's
`completed` overwrite `human_requested` would mean a supplier who asked for a
person gets phoned again tomorrow.

## 5. Structured outputs via the Responses API, not JSON mode

*(OpenAI API docs, consulted 2026-09-03. SDK `openai==3.7.0`.)*

`client.responses.parse(model=…, input=[…], text_format=PydanticModel)` constrains
the model at decode time. The JSON matches the schema rather than tending to,
which removes the retry loop entirely, and with it the class of bug where a
malformed response is silently treated as an empty one.

**Cost:** the schema has to be expressible in the strict subset. That is why the
wire schemas in `adapters/openai/schemas.py` are flatter than the domain
entities, and why every value comes back as a string.

## 6. The model returns strings; the adapter parses them

`WireConfidenced.value` is `str | None` even for money and dates.

Models emit `1.234,50`, `1234.50` and `€ 1.234,50` depending on the source
document. Asking the model to normalise and hoping is worse than parsing it here
with Italian conventions in mind, `1.234,50` read as a decimal point is one euro
twenty-three, three orders of magnitude out and entirely plausible-looking.

Everything unparseable becomes absent with a reason, never zero. **A zero price is
a number someone might act on.**

## 7. Confidence is per field, not per quote

A model can read a total cleanly off a PDF while guessing the VAT rate from the
same page. One number for the whole quote loses exactly the information a
reviewer needs.

`ExtractedQuote.overall_confidence` is the **weakest link, not the average**:
averaging lets nine confident fields hide one that was guessed.

The threshold is 0.75 and deliberately high. The cost of a wrong price reaching a
professional is far greater than the cost of someone glancing at it.

## 8. The comparison summary never calls the model

Both `OpenAIExtractor.summarise_comparison` and `MockAI.summarise_comparison`
delegate to the pure `compare()` in `domain/comparison.py`.

The prompt asked for "a customer-readable comparison summary". The fail-safe
reading is that the model already did the hard part, reading four different
documents into one shape, and turning four totals into a sentence is arithmetic.
**A model asked to write prose about numbers will eventually write a number that
was not in them**, and this particular prose is figures a professional acts on.

Side effects that turned out to matter: the summary is free, so the console can
refresh it after every reply; and mock and production cannot disagree about it.

## 9. Missing items are the most important field on the document

`missing_items` is what the supplier did *not* quote for. The extractor cannot
know it from the supplier's document alone, so the professional's requested items
are passed in with every extraction request.

A quote that is cheap because it omits half the job looks best in a comparison
table. That is the most expensive way to be wrong here, and it is why the summary
leads with "2 of 3 do not cover everything" before it says which is cheapest.

## 10. Prompts are versioned files, not string literals

`prompts/extract_quote.v1.md`, `interpret_request.v1.md`, `supplier_call.v1.md`.
The version is recorded on every extraction (`model` reads `gpt-5.6+extract_quote.v1`).

Changing the wording changes what the model reports, and a quote extracted last
month has to stay explicable by the prompt that produced it. Files also mean a
non-programmer can review the Italian.

## 11. Webhooks are signed, idempotent, and fast, in that order

- **Signed.** Every request is verified against the auth token before anything
  reads its body. The URL is rebuilt from `public_base_url`, not from the request
  host: behind a tunnel or a proxy the Host header is not what Twilio signed.
  The app **refuses to start** if real credentials are present while signature
  validation is disabled.
- **Idempotent.** Everything keys on the provider's own id. A redelivery is
  acknowledged without being acted on twice, and the status merge refuses to let
  a late `sent` undo an earlier `delivered`.
- **Fast.** Nothing in a webhook does the work. Twilio retries anything it does
  not get a prompt 200 for, so the handler stores the fact and hands off.

The relay websocket is the exception to signing: Twilio does not sign websocket
upgrades. The per-call token is the credential instead, random, only ever given
to Twilio, checked against a call actually in flight, and invalid the moment that
call resolves.

## 12. Italian opt-out words are honoured, not just English ones

Meta honours STOP, UNSUBSCRIBE and friends on a business sender. It does not
honour `BASTA`, `ALT`, `CANCELLA`, `CANCELLAMI`, `ANNULLA`, `DISISCRIVIMI`.

An Italian tradesperson typing BASTA means exactly what an English one typing
STOP means. Handled at the webhook, before anything else reads the message.

## 13. Backoff, rate limiting and the circuit breaker are three things

They get conflated. They answer different questions:

- **Backoff**, how long before I retry *this one*? Per attempt, exponential,
  capped, no jitter. No jitter because this canvasses a handful of suppliers from
  one process, so there is no herd to spread, and a deterministic delay is one a
  test can assert and an operator can predict.
- **Rate limiting**, how fast may I send *at all*? Per provider, minimum
  spacing rather than a token bucket: a bucket lets a burst through, and a burst
  is what gets a new WhatsApp sender throttled.
- **Circuit breaker**, is this provider up? After enough consecutive failures,
  stop attempting. Half-open after the reset window, so recovery needs no timer
  and the first request after it becomes the probe for free.

They live in `app/resilience.py` and are applied in `app/outreach.py`, where the
whole job is visible, not inside an adapter that can only see one send.
Deliberately **not** a decorator: wrapping hides which of the three fired, and
"the supplier was skipped" has to be explicable in the Ops Console.

## 14. The follow-up policy is nudge, call, close

4 hours → 8 hours → 24 hours, configurable, measured from the **last contact**
rather than from the order so a supplier contacted late in a round still gets its
full window. `OutreachSettings` refuses to start unless the steps escalate.

Each supplier walks its own state machine (`to_contact → contacted →
awaiting_reply → replied → quote_extracted | discarded | error`) because six
suppliers progress independently and an order must not wait on the slowest.

Terminal states are never left. A late reply from a supplier we already gave up on
must not produce a seventh quote after the professional has been offered the best
of six.

## 15. A nudge goes out on the channel that supplier actually uses

A web shop has no phone to text. Chasing it means re-asking its own agent.
Sending a WhatsApp message to a supplier who never gave a number is not a nudge,
it is a failure dressed as one, which is exactly what it looked like in the
tests before this was fixed.

## 16. The cost ceiling is checked before dispatch, never during

€2.00 per order, with a €0.20 reserve. A real round is perhaps thirty cents;
anything approaching two euro means something is looping.

The reserve exists because a job that stops halfway through a phone call has paid
for the call and learned nothing. Started work finishes; nothing new starts.

The ledger is itemised per order and stores token counts alongside euros, because
the prices are estimates and "why did this cost four euro?" has a specific answer:
three calls and forty thousand tokens.

Voicemail and no-answer are charged too. They cost money, and they are what a
runaway job produces most of.

## 17. Recording a call requires a played disclosure, enforced by the domain

`CallSession` refuses to validate with a `recording_storage_key` and
`disclosure_played=False`. In Italy recording a call without telling the other
party is not lawful, and the sentence that makes it lawful is fixed wording the
agent is not free to rephrase.

The opening line generally is not generated: it says who is calling (an
assistant, not a person) and whether the line is recorded. Neither is a model's
to soften.

## 18. Mock canvassing now runs through the same extraction as the real path

A mock supplier agent's artifact, the portal page, the email body, the call
transcript, goes through `ctx.ai.extract_quote` exactly as a supplier's WhatsApp
reply does.

Without this the comparison table would be empty in every demo, because the mock
path produced `Quote` entities and never `ExtractedQuote`. More importantly it
means the mock path and the real path converge instead of being two systems that
happen to share a screen.

## 19. `OutboundMessage` addresses a professional **or** a supplier

Previously `professional_id` was required. Nudging a supplier needs the same
field for a different party, and the 24-hour session window and the consent
record are both **per conversation**, a conversation with a supplier is not the
conversation with a professional. Keying either on the professional alone would
let a supplier's reply reopen somebody else's window.

Now exactly one of the two is required, and `counterparty_id` is what the window
and consent lookups use.

## 20. The console is designed to make a bad extraction obvious

Not to make the product look clever. A quote read at 62% confidence looks
different from one read at 95%; a missing field shows *why* it is missing rather
than a blank cell that reads as zero; a quote missing half the order is marked
before it is priced.

Two operator actions, deliberately narrow:

- **Override** corrects the two fields a person can check against the source in
  seconds. Stored at full confidence with the operator named as the evidence, and
  the model's original reading is kept in the audit output, erasing it would make
  the next argument about a price unresolvable. Review flags are recomputed, so a
  corrected total clears the warning it caused while a still-incomplete quote
  stays flagged for the reason it was actually flagged.
- **Takeover** ends one supplier's automation, per supplier rather than per
  order, because the reason is almost always one awkward conversation rather than
  a broken job. A reason is required: an unexplained takeover is invisible to
  whoever picks it up.

## 21. Dependencies added, pinned exactly

| Package | Version | Why |
|---|---|---|
| `twilio` | 9.11.0 | WhatsApp, voice, and `RequestValidator` for webhook signatures |
| `openai` | 3.7.0 | Responses API with `text_format` structured outputs |
| `python-multipart` | 0.0.32 | Twilio posts webhooks form-encoded; FastAPI needs this to read them |
| `pyyaml` | 6.0.3 | The WhatsApp template file |
| `requests` | (via twilio) | Downloading media before its URL expires; adds nothing new |

`twilio` and `requests` ship no `py.typed`, so `pyproject.toml` carries
`ignore_missing_imports` overrides for them. Nothing else is untyped.

## 22. Documentation consulted

All on 2026-09-03. Training data is stale on all of these and was not relied on.

- Twilio Programmable Messaging, WhatsApp senders, the 24-hour session window,
  template categories, status callbacks, `MessageSid` semantics
- Twilio Programmable Voice, `calls.create`, answering-machine detection, status
  callback events, `time_limit`
- Twilio ConversationRelay, the TwiML element, the websocket message protocol
  (`setup`, `prompt`, `interrupt`, `dtmf`, `error`; `text` and `end` outbound)
- Twilio Security, `RequestValidator`, what the signature covers
- OpenAI Responses API, `responses.parse`, `text_format`, `output_parsed`,
  refusals, image and file inputs as data URLs
- OpenAI structured outputs, the strict schema subset
