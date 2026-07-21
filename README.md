# Shopfloor

Server-rendered production queue for personalized-product makers.

## Run

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/dashboard, then generate demo data:

```bash
curl -X POST 'http://127.0.0.1:8000/simulate/orders?n=5'
```

With `SIM_MODE=true`, the queue worker inspects only the generated sample images locally: `good` passes, while `blurry`, `low-res`, and `face-near-edge` are held with a customer re-upload link. Every customer upload requires `OPENAI_API_KEY` for GPT-5.6 vision QC; without it, the order remains visibly pending instead of passing automatically.

The dashboard is split into two levels: an owner summary that answers what needs attention, and the production queue where an order can be opened and advanced. Stage targets use Europe/Madrid business time (Monday-Friday, 09:00-18:00): ready to print 6 hours, printed 6 hours, and pressed 12 hours. First photo response has a 2-minute elapsed-time target; photo holds get a simulated reminder every 24 hours and request personal follow-up after the third reminder. Shipped orders can be marked delivered in the demo, while the production cycle ends when the order is handed to the carrier.

Real image QC uses `gpt-5.6-terra` and is capped by `MAX_REAL_QC_CALLS` (20 by default). The cap is enforced in the app before an API call is made.

Customer re-uploads accept verified JPEG, PNG, or WebP images up to 10 MB. Override the byte limit with `MAX_UPLOAD_BYTES`.

## Hosted demo

`render.yaml` defines a single-instance Render web service with a 1 GB persistent
disk. SQLite, customer uploads, and structured logs live under `/var/data`. A
fresh deployment seeds 12 active sample orders and 16 completed orders so the
seven-day production and delivery metrics are immediately useful. Public chaos
controls are disabled.

Render starts the service with:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Datadog-ready run

Install and run the Datadog Agent on the Mac mini, then start the app with APM tracing enabled:

```bash
DD_SERVICE=shopfloor DD_ENV=development ddtrace-run uvicorn app.main:app --reload
```

Set `DD_ENV` before `ddtrace-run` so APM traces and custom metrics share the same environment tag. The app writes structured JSON logs to `logs/app.jsonl` and sends the business metrics named in `SPEC.md` to the local DogStatsD Agent. It also emits `maker.orders.by_status` for the live production funnel and `maker.qc.inspected` as the denominator for QC rejection rate.

For a hosted Render deployment, set `DD_HTTP_ENABLED=true`, `DD_SITE=datadoghq.eu`, `APP_BASE_URL` to the public service URL, and add `DD_API_KEY` only in Render's environment settings. The app then publishes aggregate stage counts, overdue counts, oldest-stage age, and seven-day production/delivery metrics directly to Datadog. Per-order attention records are logs—not high-cardinality metric tags—and include an `order_url` deep link back to Shopfloor without customer names.

## Test

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```
