# Invoice Review API

A compact document operations system built with **Python, FastAPI, SQLite, and a browser UI**. It accepts text from an OCR process, extracts invoice fields, flags missing or inconsistent values, and requires a human decision. Every intake, correction, and decision has an audit event.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB) ![FastAPI](https://img.shields.io/badge/FastAPI-API-009688) ![Tests](https://img.shields.io/badge/tests-pytest-6C8EAD)

## Why I built this

Enterprise document workflows need more than extraction. A useful system also needs idempotent intake, validation, duplicate checks, an exception queue, human review, and traceable decisions. This project demonstrates those pieces with synthetic data and no external API key.

## Try it in three minutes

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the review UI, or [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for the API. Click **Load sample**, then ingest it. You can also paste the text in [`examples/invoice-review.txt`](examples/invoice-review.txt) to see an amount mismatch.

Run tests with `pip install -r requirements-dev.txt && python -m pytest -q`.

To run the API in Docker: `docker build -t invoice-review-api . && docker run --rm -p 8000:8000 invoice-review-api`. Data inside this container is temporary unless you mount a volume at `/data`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/invoices` | Ingest text with a unique `source_id`; exact retries return the existing record |
| `GET` | `/api/invoices` | List invoices; optional `status` filter |
| `GET` | `/api/invoices/{id}` | Get extraction, issues, and audit events |
| `PATCH` | `/api/invoices/{id}` | Correct extracted fields and rerun checks |
| `POST` | `/api/invoices/{id}/decisions?action=approve` | Approve only when checks pass |
| `POST` | `/api/invoices/{id}/decisions?action=reject` | Reject with a reason |
| `GET` | `/api/metrics` | Review queue counts |

The default SQLite file is `invoice_review.db`; set `INVOICE_DB` to override it. This demo intentionally uses a single local database and has **no authentication**. Run it locally; add identity, permissions, migrations, and a production database before hosting it publicly.

## Design notes

- Amounts use `Decimal`, so `subtotal + tax = total` is checked at currency precision.
- A duplicate is flagged when vendor and invoice number match an existing record. It remains a review issue rather than being silently discarded.
- OCR text can be wrong or incomplete. The extractor leaves unknown values empty; reviewers correct fields before approval.
- The sample invoices are fictional. No employer data or proprietary code is included.

## Next steps

PDF/OCR ingestion, line-item matching, vendor master data, reviewer authentication, and Postgres migrations would be the natural extensions.
