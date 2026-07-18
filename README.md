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

Real image QC uses `gpt-5.6-terra` and is capped by `MAX_REAL_QC_CALLS` (20 by default). The cap is enforced in the app before an API call is made.

Customer re-uploads accept verified JPEG, PNG, or WebP images up to 10 MB. Override the byte limit with `MAX_UPLOAD_BYTES`.

## Datadog-ready run

Install and run the Datadog Agent on the Mac mini, then start the app with APM tracing enabled:

```bash
DD_SERVICE=shopfloor ddtrace-run uvicorn app.main:app --reload
```

The app writes structured JSON logs to `logs/app.jsonl` and sends the business metrics named in `SPEC.md` to the local DogStatsD Agent. It also emits `maker.orders.by_status` for the live production funnel and `maker.qc.inspected` as the denominator for QC rejection rate.

## Test

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```
