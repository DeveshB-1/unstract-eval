# unstract-eval

Golden-set accuracy testing for [Unstract](https://github.com/Zipstack/unstract) API deployments.

You give it a folder of documents and a folder of hand-checked JSON answers. It runs every document through your API deployment, scores each extracted field against the answer, and tells you which fields are wrong. Pass `--profile` more than once and it runs the same set through several LLM profiles (via the deployment's `llm_profile_id` override) and puts accuracy next to cost per document.

It exits non-zero when accuracy drops below a floor or falls against a saved baseline, so it can sit in CI and catch a prompt edit or model swap that quietly breaks a field.

## Why this is useful for Unstract users

Prompt Studio is where you build an extraction. LLMChallenge checks two models against each other on each run. What I couldn't find in the repo or docs is the offline question a team asks before it ships a change: *on the 50 documents we already know the answers for, did this change make anything worse?* (I searched `Zipstack/unstract` for "ground truth" and "golden" and found nothing that does this.)

Exact string matching is the wrong tool for that question. Extraction output that is correct often differs from a hand-written answer:

| golden | extracted | exact match | unstract-eval |
|---|---|---|---|
| `2026-03-14` | `03/14/2026` | wrong | match (date) |
| `244850.0` | `2,44,850.00` | wrong | match (number, Indian grouping) |
| `Northwind Supplies LLC` | `Northwind Supplies LLC.` | wrong | match (normalised string) |
| line items in order A, B | B, A | wrong | match (lists are order-insensitive) |
| `PO-78010` | `78010` | wrong | wrong |

So the scorer normalises by field type first, then compares, and reports what an exact match would have said next to it.

## What it scores

- **Types**: `number` (currency symbols, thousands and lakh grouping, `(42.10)` negatives, tolerance), `date` (ISO, `03/14/2026`, `May 20th, 2026`, `18 Apr 2026`, with `MDY`/`DMY` order), `bool`, `string` (case, whitespace and trailing punctuation), `fuzzy` (similarity threshold), `exact`. Types are inferred from the golden value and can be overridden per field.
- **Nested objects** are scored leaf by leaf (`line_items[*].amount`).
- **Lists** are matched order-insensitively. A missed item counts every one of its fields as `missing`; an item the model invented counts as `extra`, so hallucinated rows cost accuracy.
- **Nulls**: a golden `null` matches an empty or absent value.
- **Cost**: sums every `cost_in_dollars` in the result metadata (requests are sent with `include_metadata=true`).

Per-field rules live in a small JSON file:

```json
{
  "date_order": "MDY",
  "number_tolerance": 0.01,
  "fields": {
    "vendor_name": {"type": "fuzzy", "threshold": 0.9},
    "po_number": {"type": "string"},
    "line_items[*].description": {"type": "fuzzy", "threshold": 0.85}
  },
  "ignore": ["debug.*"]
}
```

## Quickstart

```bash
uv sync
# offline demo against the bundled mock deployment (no key needed)
uv run unstract-eval run \
  --docs examples/invoices/docs --golden examples/invoices/golden \
  --config examples/invoices/eval.json --mock examples/invoices/mock_outputs.json \
  --profile profile-a --profile profile-b --min-accuracy 0.9 \
  --json reports/demo.json --markdown reports/demo.md
```

Against a real deployment (the API URL and key from the deployment's page in Unstract; profile IDs from Prompt Studio > Settings > LLM Profiles):

```bash
export UNSTRACT_API_URL=https://us-central.unstract.com/deployment/api/<org>/<api_name>/
export UNSTRACT_API_KEY=<deployment key>
uv run unstract-eval run --docs ./pdfs --golden ./golden --config eval.json \
  --profile <profile-uuid-1> --profile <profile-uuid-2> --json reports/today.json
```

Golden files are matched by name: `pdfs/inv-1001.pdf` is scored against `golden/inv-1001.json`.

### As a CI gate

```bash
uv run unstract-eval run --docs ./pdfs --golden ./golden --config eval.json \
  --baseline reports/main.json --max-drop 0.02 --min-accuracy 0.95
```

Exit `1` if any profile is under `--min-accuracy`, or if overall accuracy or any single field fell more than `--max-drop` against the baseline. Exit `2` for usage or API errors.

## How it talks to Unstract

`client.py` follows the execute/status contract in `Zipstack/unstract-python-client`'s OpenAPI spec (`specs/docstudio-oss.json`):

- `POST {api_url}` with multipart `files`, `timeout=0` (queue and return), `include_metadata=true`, optional `llm_profile_id`.
- `GET` on `status_api` (or `?execution_id=`) until it answers 200. 422 means still running unless `status` is an error state.
- Status reads are one-shot: once a finished result is read, later reads answer 406. The client keeps the payload from the first 200 and reports a 406 as a clear error instead of retrying.
- The docs show more than one shape for a file's `result` (`{"output": {...}}`, `{"input_file", "result"}`, or a JSON string), so `extract_output` accepts each.

`mock.py` is an `httpx.MockTransport` that plays the same contract (PENDING, then 422, then 200 once, then 406) and checks the Bearer key. The tests and `--mock` run against it.

## Demo run

This is the offline demo. **The two profiles' outputs and costs are simulated** (`examples/invoices/mock_outputs.json`): I wrote them to look like the mistakes extraction models make (format drift, a dropped line item, a due date copied from the invoice date, an invented row). The scoring, gating and HTTP flow are real code paths. I haven't run it against a live Unstract deployment because I don't have a key.

```
$ uv run unstract-eval run --docs examples/invoices/docs --golden examples/invoices/golden \
    --config examples/invoices/eval.json --mock examples/invoices/mock_outputs.json \
    --profile profile-a --profile profile-b --min-accuracy 0.9 --json reports/demo.json --markdown reports/demo.md
running 5 documents with profile profile-a ...
running 5 documents with profile profile-b ...
# Extraction eval: 5 documents

| profile | field accuracy | exact-match would say | docs fully correct | wrong fields | cost/doc |
|---|---|---|---|---|---|
| profile-a | 86.3% (82/95) | 69.5% | 1/5 | 13 | $0.0021 |
| profile-b | 97.9% (92/94) | 94.7% | 3/5 | 2 | $0.0094 |

## Per-field accuracy (worst first)

| field | profile-a | profile-b |
|---|---|---|
| `line_items[extra]` | 0% | - |
| `line_items[*].quantity` | 73% | 100% |
| `due_date` | 80% | 100% |
| `po_number` | 100% | 80% |
| `vendor_name` | 100% | 80% |
| `total` | 80% | 100% |
| `subtotal` | 80% | 100% |
| `line_items[*].description` | 82% | 100% |
| `line_items[*].unit_price` | 82% | 100% |
| `line_items[*].amount` | 82% | 100% |
| `invoice_date` | 100% | 100% |
| `currency` | 100% | 100% |
| `customer_name` | 100% | 100% |
| `invoice_number` | 100% | 100% |
| `tax` | 100% | 100% |

## Wrong fields: profile-a (13)

| doc | field | status | expected | got |
|---|---|---|---|---|
| inv-1002.txt | `due_date` | mismatch | 2026-05-01 | 2026-04-01 |
| inv-1003.txt | `line_items[1].description` | missing | GST filing support | null |
| inv-1003.txt | `line_items[1].quantity` | missing | 3 | null |
| inv-1003.txt | `line_items[1].unit_price` | missing | 7500.0 | null |
| inv-1003.txt | `line_items[1].amount` | missing | 22500.0 | null |
| inv-1004.txt | `line_items[2].description` | missing | Drayage | null |
| inv-1004.txt | `line_items[2].quantity` | missing | 2 | null |
| inv-1004.txt | `line_items[2].unit_price` | missing | 410.0 | null |
| inv-1004.txt | `line_items[2].amount` | missing | 820.0 | null |
| inv-1004.txt | `subtotal` | mismatch | 4895.0 | 4075.0 |
| inv-1004.txt | `total` | mismatch | 4895.0 | 4075.0 |
| inv-1005.txt | `line_items[0].quantity` | mismatch | 6 | 5 |
| inv-1005.txt | `line_items[+2]` | extra | null | {'description': 'Delivery and assembly', 'qua... |

## Wrong fields: profile-b (2)

| doc | field | status | expected | got |
|---|---|---|---|---|
| inv-1002.txt | `vendor_name` | mismatch | Kestrel Cloud Services | Kestrel Cloud Services Pvt Ltd |
| inv-1004.txt | `po_number` | mismatch | PO-78010 | 78010 |

## Gate

- FAIL: profile-a: accuracy 86.3% below --min-accuracy 90.0%
$ echo $?
1
```

Reading it: for `profile-a`, exact matching would mark 29 of 95 fields wrong. 16 of those 29 are formatting (dates, number formats, casing, an empty string for a null PO, a dropped "Inc"). The 13 real errors are mostly line items (two dropped rows, one wrong quantity, one invented row), plus a copied due date and totals that follow from the dropped row. `profile-b` gets 2 fields wrong at roughly 4.5x the cost per document, which is the trade-off a team would actually want a number for.

## Tests

```
$ uv run pytest -q
...................
19 passed in 0.09s
```

Covers the normalisers, list matching, rule patterns, the polling client (422/200/406, auth, `llm_profile_id` on the wire, result shapes, cost extraction) and the CLI end to end including the baseline gate.

## Layout

```
src/unstract_eval/
  client.py   execute + poll one API deployment
  score.py    type-aware field comparison
  report.py   per-profile summary, CI gate, Markdown
  cli.py      unstract-eval run
  mock.py     offline Unstract deployment for tests and demo
examples/invoices/  5 invoices, golden JSON, eval.json, simulated outputs
```

## Limits

- No live run yet (no key). The client is written to the published spec and the mock, so the first real run may turn up a response detail the docs don't show.
- One file per request. Batching several files per execution would be faster on large golden sets.
- List items are paired greedily by best leaf match, which is fine for invoices and receipts and could mis-pair on long tables of near-identical rows.

Built by Devesh B (github.com/DeveshB-1).
