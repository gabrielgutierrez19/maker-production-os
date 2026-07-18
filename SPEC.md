
# Sprint: OpenAI Build Week, Shopfloor

**Sprint Type**: Build (hard deadline)
**Status**: Active
**Created**: 2026-07-14
**Deadline**: Submission Tue Jul 21 5:00pm PDT (2:00am Wed Madrid). Target: submit by 10pm Madrid Tuesday.
**Areas served**: job search (Datadog interview Mon Jul 20, OpenAI FDE application, ElevenLabs application), wife's magnet-shop operation (customer zero)
**Track**: Work & productivity tools
**Repo**: `~/maker-production-os` (product name: Shopfloor, decided 2026-07-16)

---

## The pitch (for the video and README)

Shopify runs your store. Excel runs your production. For every maker selling physical, personalized products, the real world (what is printed, pressed, ready, late) lives in a spreadsheet that never talks to the store. This is the production layer: it pulls orders from Shopify, inspects every customer photo with GPT-5.6 vision before anything prints, runs the queue the owner taps through, measures where production is slow, and calls the owner (literally, with a voice) when something breaks. Built with Codex, demonstrated on a real business: my wife's personalized photo-magnet company.

Positioning rule: build the specific, claim the general. Customer-zero instance hard-coded. "Configurable stages per craft", multi-tenant, delivery tracking, and support automation are roadmap slides, not code.

Honesty rule for the video: never claim "no one has this". Katana (Shopify manufacturing ERP) and Craftybase (handmade inventory/COGS) exist. The claim: those are ERP-shaped and team-sized. Nothing is AI-native, does perceptual QC on customer files, or proactively calls a one-person shop.

## What GPT-5.6 does (core, per contest rules)

1. **Vision QC gate**: judges every customer photo for printability at 50mm magnet size before production.
2. **Incident copilot**: investigates Datadog alerts (pulls logs, metrics, deploy events) and writes the root-cause briefing.
3. **Morning ops briefing**: daily summary of overnight orders, flags, backlog.

Codex builds the whole thing (judged on depth of Codex usage, log `/feedback` Session ID daily).

---

## Stack

- Python 3.12, FastAPI, SQLite, Jinja2 + htmx (server-rendered, no JS framework)
- Worker: asyncio background task inside the app (no Celery/RQ)
- `ddtrace` (APM), DogStatsD (custom metrics), structured JSON logs to file (Agent tails it)
- OpenAI SDK (GPT-5.6 text + vision), ElevenLabs SDK (TTS), optional Twilio call = stretch only
- Email in demo mode = rendered to log/console. Real SMTP (Resend) optional, not required for submission
- Runs on the Mac mini. `.env` for keys, never committed

## Data model (SQLite)

- `orders`: id, source (shopify | event | sim), shopify_order_id, customer_name, email, package, status, created_at, sla_due_at
  - status flow: received → qc → on_hold_photo → ready_to_print → printed → pressed → shipped
- `photos`: id, order_id, file_path, qc_status (pending | pass | fail), qc_reasons (JSON), customer_message, replaced_by
- `stage_events`: id, order_id, from_status, to_status, at (the analytics backbone, every transition timestamped)
- `reupload_tokens`: token, order_id, photo_id, expires_at, used_at

## Endpoints

- `POST /webhooks/shopify/orders`: accepts real Shopify orders/create JSON, verifies HMAC (skippable in sim mode). Photos read from line item properties (upload-app URLs) or sim file paths
- `POST /simulate/orders?n=N`: demo generator, N orders in exact Shopify payload shape with sample photos (mix of good, blurry, low-res, faces-near-edge)
- `GET /dashboard`: the queue board. Columns per status, one tap advances an order, flags visible
- `POST /orders/{id}/advance`
- `GET|POST /reupload/{token}`: the one customer-facing page. Shows the flagged photo + reason, accepts replacement, re-queues QC
- `POST /webhooks/datadog`: monitor alert intake → incident copilot
- `POST /chaos/{mode}`: demo switches. `poison` (inject an upload that crashes the worker), `surge` (40 orders at once), `slow` (add latency)
- `GET /health`

## The two AI prompts (Codex implements, Gabriel reviews wording)

**Vision QC** (per photo, GPT-5.6 vision, JSON output):
> You are the print-quality inspector for a shop that heat-presses customer photos onto 50mm magnets. Judge THIS photo for printability at that size: sharpness (especially faces), effective resolution for a 50x50mm print, exposure (too dark/blown out), and crop risk (faces or subjects that a square crop would cut). Return JSON: {"verdict": "pass"|"fail", "reasons": [...], "customer_message": "..."} . customer_message is a warm, plain-Spanish, one-sentence explanation with a concrete suggestion, written to the customer.

**Incident copilot** (on Datadog webhook, GPT-5.6):
> You are the on-call SRE for a small production pipeline. Alert payload: {...}. Evidence: recent error logs {...}, metric series {...}, recent deploy/chaos events {...}. In exactly 3 sentences state: what is happening, the most likely root cause with the evidence, and one concrete recommended action. Then a one-line spoken headline. No hedging.

Briefing text → ElevenLabs TTS → mp3 saved + auto-played. Twilio outbound call only if everything else is done Friday.

## Datadog setup (this is also the interview prep)

- Agent on the Mac mini: APM (`ddtrace-run`), log collection (tail the app's JSON log), DogStatsD
- **Custom business metrics**: `maker.orders.created`, `maker.qc.rejected`, `maker.stage.cycle_seconds` (tagged by stage), `maker.backlog.oldest_order_age_hours`, `maker.qc.worker.heartbeat`
- **Monitors** (3):
  1. APM error rate > 5% over 5 min → webhook
  2. `maker.backlog.oldest_order_age_hours` above threshold (demo threshold minutes, real threshold 48h) → webhook
  3. Heartbeat missing 5 min (worker died) → webhook
- **Dashboard**: order funnel by status, cycle time per stage, QC rejection rate, error rate + latency, backlog age
- Deploys/chaos flips emit Datadog events so the copilot can correlate ("began 90 seconds after...")

## Demo video script (3 min, public YouTube, voiceover must cover Codex AND GPT-5.6 usage)

1. 0:00 Cold open on a real messy spreadsheet: "this is production management for makers today"
2. 0:20 Instagram-style ad → Shopify order lands (simulated webhook, real payload) → order appears in the queue
3. 0:40 QC moment: one photo flagged, show the reason + the customer swap email + the re-upload page → replacement passes → order releases
4. 1:20 Owner taps through print → press → ship. Cut to Datadog dashboard: funnel, cycle times, "where am I slow"
5. 1:50 Chaos: poison upload kills the worker (or surge blows the SLA) → monitor fires → **the voice call plays**: cause + action → fix → recovery on the dashboard
6. 2:30 Roadmap beat (one breath): configurable stages for any maker, delivery tracking, and "where is my order" support answering itself
7. 2:40 Codex screen: session doing the heavy lifting, where it accelerated, GPT-5.6 usage recap

## Day-by-day plan

- **Wed 15 AM**: scaffold repo, data model, sim + Shopify webhook endpoints, dashboard skeleton. **PM**: QC worker + vision gate + re-upload flow, end to end on sample photos
- **Thu 16 AM**: Datadog (Agent, APM, logs, custom metrics, dashboard, 3 monitors). **PM**: incident copilot + ElevenLabs TTS + chaos switches + morning briefing
- **Thu night: KILL CHECKPOINT.** Not demo-able → freeze as the Datadog practice app (interview value survives fully), skip submission
- **Fri 17**: polish dashboard, rehearse the demo flow twice, capture screenshots for Monday's interview, record a backup video take
- **Sat 18**: final video + README (setup, sample data) + Codex-usage doc + `/feedback` Session ID
- **Sun 19**: FREEZE. Datadog interview rehearsal only (CapM talk track, product names, this project as the story)
- **Mon 20**: interview. No code
- **Tue 21**: submission polish, submit by 10pm Madrid

## Submission checklist (from the rules)

- [ ] Built with Codex + GPT-5.6, track: Work & productivity tools
- [ ] 3-min public YouTube video, voiceover covers Codex AND GPT-5.6 usage
- [ ] Repo public (or shared with testing@devpost.com and build-week-event@openai.com)
- [ ] README: setup instructions + sample data (judges may run it)
- [ ] Doc: where Codex accelerated the workflow
- [ ] `/feedback` Codex Session ID from the primary session (log it every day, do not reconstruct Saturday)

## Guardrails

- NO printer integration, NO multi-tenant, NO carrier/delivery API, NO support inbox. Roadmap slides only
- Keep the business name OUT of all public submission materials (Devpost story, video, README, repo, UI screens). Decided 2026-07-16
- OpenAI + ElevenLabs spend: small batches, sample photos reused, TTS only on final briefing texts
- Keys in `.env`, `.gitignore` from commit one (repo may go public)
- Datadog interview (Mon) outranks everything here. Any conflict resolves toward the interview

## Interview tie-ins to capture along the way (for Mon Jul 20)

- Screenshot: the dashboard with business metrics + a fired monitor + the recovery
- The line: "I put business observability on a real company last week: custom metrics, SLO-style monitors on order age, and the owner gets a voice briefing when production breaks"
- The architecture walkthrough of THIS system (webhook → worker → queue → monitors → copilot) as a warm-up twin of the CapM talk track

---

