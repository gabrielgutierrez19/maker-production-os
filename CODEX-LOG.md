# Codex build log

**Primary Codex Session ID:** `019f6142-952d-7b62-b5ab-96e2ffee93b0`

This is the primary build session used for the OpenAI Build Week submission. It records where Codex materially accelerated the work and where Gabriel changed the product direction.

## 1. Production foundation

Codex read the product specification before implementation, challenged the initial architecture, and kept the deliberately small Python/FastAPI/SQLite/Jinja2 stack. It implemented the exact initial data model, real Shopify payload parsing, HMAC verification, deterministic order simulation, and the first server-rendered queue.

**Acceleration:** One continuous workflow covered schema, webhook security, realistic fixtures, templates, and end-to-end tests instead of treating each as a separate handoff.

## 2. Photo quality and customer recovery

Codex implemented the GPT-5.6 vision contract, structured verdict parsing, photo-failure messaging, expiring one-use re-upload tokens, image-content validation, API-call caps, and fail-closed behavior when inspection is unavailable.

**Gabriel's correction:** A blurry face and an unrelated rendering initially passed the early simulation path. Gabriel flagged that this made the quality claim untrustworthy. The flow was changed so only known sample fixtures use deterministic outcomes; unknown customer files require the real quality service and remain pending if it cannot run.

**Gabriel's correction:** API cost and subscription expectations were raised explicitly. The result was a controlled public demo, a hard cap on real quality calls, and transparent pending states rather than hidden fallback approval.

## 3. Operational safety and observability

Codex added structured logs, Datadog APM/metric tagging, queue and worker metrics, reversible chaos controls, signed Datadog webhooks, evidence collection, and a three-sentence incident briefing. Tests cover malformed and unauthorized alert payloads, worker failures, slow mode, recovery, and incident display.

**Acceleration:** Codex traced failures across the application, local Agent configuration, Datadog monitors, and webhook delivery instead of optimizing only the code side.

## 4. Owner-oriented redesign

The first Datadog dashboard was technically correct but graph-heavy. Gabriel reframed the user as a non-technical business owner who needs to know what is broken and what to do. Codex redesigned both Shopfloor and Datadog around:

- What needs attention now
- Current orders and overdue work by stage
- The oldest order in each stage
- One dominant seven-day production-cycle measure
- Supporting fulfillment, customer-wait, carrier-delivery, and slow-order measures
- Technical health collapsed until it is needed

Codex implemented Monday-Friday business-hour calculations, stage targets, 24-hour photo reminders, three-reminder follow-up, simulated delivery, order drill-down pages, and safe aggregate Datadog metrics. Per-order identifiers were kept out of metric tags to avoid high-cardinality telemetry.

**Gabriel's correction:** Production time had to exclude customer photo waiting. Carrier delivery also had to be separated from the production cycle because it is measured but not controlled by the shop.

## 5. Deployment and security

Codex prepared the public GitHub repository and Render Blueprint, added persistent storage, verified the hosted app, migrated the Datadog webhook from a temporary tunnel to the permanent service, and tested public order drill-downs.

**Gabriel's decision:** API-key handling remained manual. Gabriel created and pasted the dedicated Datadog key directly into Render; Codex verified metric arrival without reading or displaying the credential.

## 6. Scope discipline

Gabriel and his wife identified an additional personalization-proof workflow: personalized frame design, customer approval, and revision. Codex separated the storefront preview from the production workflow and proposed an optional branch after photo quality. The team chose to document it in the roadmap rather than destabilize the working demo.

## Result

The primary session produced and verified:

- A public working application
- Shopify-shaped ingestion and simulation
- GPT-5.6 photo quality and incident-copilot contracts
- Customer photo recovery
- Owner production workflow and operational analytics
- Datadog business monitoring
- Thirty passing automated tests
- Render deployment and submission documentation

Codex's largest contribution was not raw code generation. It kept product decisions, implementation, verification, observability, security, and deployment in one evidence-backed loop while Gabriel supplied the real operational constraints.
