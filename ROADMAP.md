# Shopfloor roadmap

The demo proves a customer-zero production workflow for one personalized-product maker. The roadmap expands that proof without turning the current build into a premature ERP.

## Next: personalization proofs and approval

Some orders include a designed frame or graphic treatment—for example a city frame, trip theme, or baby-month marker. This should be an optional branch after photo quality:

```text
Quality check passed
        ↓
Personalization required?
   No → Ready to print
   Yes → Personalizing
              ↓
       Awaiting customer approval
          ├─ Approved → Ready to print
          └─ Changes requested → Personalizing
```

Initial scope:

- Receive the customer's selected design/template as Shopify order metadata
- Let the maker upload a proof
- Give the customer a simple Approve or Request changes page
- Record proof versions and approval events
- Exclude customer approval waiting from production cycle time
- Remind the customer after 24 hours using the existing reminder pattern

Explicitly out of the first version: a design editor, generative frame creation, complex annotations, and real-time collaboration. The storefront preview remains a Shopify concern; Shopfloor begins with the ordered design choice.

## Productization

- Authentication and role-based access
- Multi-business tenancy and configurable production stages
- Managed PostgreSQL and object storage
- Real email/SMS/WhatsApp messaging
- Carrier handoff and delivery webhooks
- Configurable calendars, targets, and escalation policies
- Customer-safe order-status pages

## Intelligence

- Suggested customer messages based on photo-quality failures
- Proof revision summaries
- Capacity forecasting and backlog prediction
- Stage bottleneck recommendations
- Morning owner briefing
- Searchable incident and production history

## Integrations

- Shopify app installation and OAuth
- Upload-app adapters
- Shipping providers
- Printer/press workflow integrations where APIs exist
- Accounting and inventory systems
