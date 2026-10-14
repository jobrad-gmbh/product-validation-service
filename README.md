# Accessory Validator

FastAPI service that runs the configured accessory validations and returns one
report containing their business results.

## Run locally

```bash
docker compose up -d postgres
docker compose --profile local-auth up -d oauth
uv sync --locked
uv run alembic upgrade head
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The API documentation is available at <http://127.0.0.1:8000/docs>.
Add the authentication settings from `.env.example` to your `.env` before
starting. Both validation endpoints require a Bearer token, including locally.
See [service authentication](AUTHENTICATION.md) for token commands, endpoint
permissions, and the IAM registration needed for deployed environments.
Set `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` in `.env` for the
Compose database. `DATABASE_URL` reuses those values to connect local commands
through `localhost`; set it differently if PostgreSQL is elsewhere. To run both the
database and API in containers, fill in `.env` with your LiteLLM and Jev
settings, then use `docker compose up --build`. Compose builds the API's database
URL from the PostgreSQL values using the `postgres` service hostname. The API
container applies migrations before starting.

For a fully containerized local run with mock authentication, set
`AUTH_ISSUER=http://oauth:9010/accessory-validator` in `.env`, then run:

```bash
docker compose --profile local-auth up --build
```

Compose reads `AUTH_ISSUER` and `AUTH_AUDIENCE` directly from configuration.
Use `localhost:9010` when the API runs on your computer and `oauth:9010` when
it runs in Docker. Mock tokens must be obtained using the same issuer hostname; see
[service authentication](AUTHENTICATION.md#local-tokens). To use real IAM, set its issuer URL instead and omit `--profile local-auth`.

Products and validation executions are saved atomically. Criterion results are
returned in the validation response but are not stored separately. LLM requests
are saved as they happen, including failed requests.
Each request stores the `product_id` and `validation_id` that caused it. These
are not foreign keys: requests from a failed validation have no stored product.

## Validate an accessory

```bash
curl -X POST http://127.0.0.1:8000/api/v1/accessories/validate \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "brand": "Example",
    "model": "Rear rack",
    "category": "bicycle transport",
    "price": "49.99",
    "origin": {
      "source": "odoo",
      "external_ref": "ACC-42"
    },
    "context": {
      "is_bawu_order": false
    }
  }'
```

`context` is optional and defaults to a non-BAWU order.

## Test an accessory validation

`POST /api/v1/accessories/validate/test` runs the same validation and returns the
same report, but stores nothing: no product, validation execution, or LLM request.

Set `include_product_information` to `true` in the request body to include a
top-level `product_information` object in the response. It contains the `summary`,
`model`, `used_web_search`, and `sources` (each with `url` and optional `title`)
used during validation. The flag defaults to `false`, which omits this field.
Including it reuses the retrieved information and makes no extra LLM requests.

It needs `llm_settings.api_key`. The server's `LITELLM_API_KEY` is never used by
this endpoint. The other LLM settings are optional. Any setting you leave out uses
the server's value.

`criterion_settings` is optional. It changes `models`, `temperature`, `max_tokens`,
`reasoning_effort`, or `timeout_seconds` for individual criteria. All requests use
the API key and base URL from `llm_settings`, so those two fields cannot be set per
criterion. Criteria
without their own settings use their default models and the general settings.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/accessories/validate/test \
  -H "Authorization: Bearer $TEST_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "brand": "Example",
    "model": "Rear rack",
    "price": "49.99",
    "origin": {"source": "odoo", "external_ref": "ACC-42"},
    "include_product_information": true,
    "llm_settings": {
      "api_key": "sk-your-own-key",
      "base_url": "https://litellm.example.com/v1",
      "models": ["gpt-luna"],
      "temperature": 0.2,
      "max_tokens": 1000,
      "reasoning_effort": "low",
      "timeout_seconds": 60
    },
    "criterion_settings": {
      "special_rules": {"models": ["glm-5.3"], "temperature": 0, "reasoning_effort": "medium"}
    }
  }'
```

Left unset, no reasoning effort is sent and the model uses its default. Allowed
values are OpenAI's (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`);
individual models support only a subset, and the request fails if a model rejects
the chosen value.

Criterion ids: `explicitly_not_leasable_type`, `explicitly_leasable_type`,
`technical_bicycle_component`, `stvzo_equipment`, `functional_unit_with_bicycle`,
`permanently_mounted`, `special_rules`.

Like other criteria, `special_rules` returns `answer` and `details`. Its answer
means eligibility under the applicable special rule: `YES` approves, `NO`
rejects, and `UNKNOWN` follows the existing strategy fallback. If no special
rule applies, its answer is `UNKNOWN`. There is no separate `leasable` field.

## Batch validation from JSONL

Put one accessory object on each nonblank line of an input `.jsonl` file:

```jsonl
{"brand":"Example","model":"Rear rack","price":79.90,"origin":{"source":"dataset","external_ref":"ACC-1"}}
{"brand":"Example","model":"Front light","price":49.90,"origin":{"source":"dataset","external_ref":"ACC-2"},"context":{"is_bawu_order":true}}
```

Run the batch script from the repository root:

```bash
export ACCESSORY_VALIDATOR_ACCESS_TOKEN="$TEST_ACCESS_TOKEN"
uv run python scripts/validate_accessories.py \
  --input /path/to/accessories.jsonl \
  --output /path/to/results.jsonl \
  --api-key "$LLM_API_KEY" \
  --endpoint http://localhost:8000/api/v1/accessories/validate/test \
  --concurrency 8
```

- `--concurrency` accepts 1–32 concurrent validations; the default is 1.
- `ACCESSORY_VALIDATOR_ACCESS_TOKEN` supplies the API Bearer token separately
  from the LLM key. `--access-token` also works but exposes the token in the
  process list. The token needs the test validate permission; renew it between
  batches if it expires.
- `--from-record 100 --to-record 200` optionally selects an inclusive, 1-based
  range. Blank lines are ignored; malformed nonblank lines count as records.
  Either bound may be used alone.
- Product information is included by default. To disable it, pass
  `--no-include-product-information`.
- `--endpoint` defaults to the local test endpoint shown above. `--timeout`
  controls the HTTP timeout in seconds and defaults to 1800.
- Each input record may include `llm_settings` and `criterion_settings`. The
  script preserves these settings, overriding `llm_settings.api_key` with the
  flag and `include_product_information` with the script's selection.
- Results are appended to the output file as requests finish, with each line
  flushed and synced to disk. Existing results are preserved; use `--overwrite`
  to replace them. Repeating a range appends duplicate results; there is no
  automatic resume or retry.

Each output line contains `record_number`, `input`, `status_code`, `result`, and
`error`. Concurrent results may arrive out of input order; use `record_number`
to match them. The API report is stored in `result`, including its product
information. Invalid input, HTTP errors, and network failures are saved in
`error`, and the remaining records continue. API keys and access tokens are redacted from saved
data. A business validation result of `INVALID` is still a successful API call.

The script exits with code 0 when all selected requests succeed, 1 when a
record fails or the batch cannot continue, and 130 when interrupted with Ctrl+C.
Completed rows remain available after interruption; in-flight requests may have
reached the server without producing a saved result.

## Development

```bash
uv run python -m pytest
uv run python -m ruff check app
uv run python -m mypy app
```

All current accessory leasability criteria use the configured LiteLLM gateway.

## Project structure

The top level separates business behavior (`domain`), external communication
(`adapters`), configuration (`config`), and application assembly (`main.py`).

```text
app/
├── main.py
├── config/
├── adapters/
│   ├── web/
│   └── llm/
└── domain/
    ├── criterion.py                  # Criterion answers and results
    ├── product.py
    ├── validation.py
    ├── validation_results.py
    ├── validation_service.py
    ├── errors.py
    └── validations/
        └── accessories/
            ├── suite.py
            └── leasability/
                ├── validation.py
                ├── strategies.py
                ├── criteria.py       # Accessory leasability criteria
                └── prompts/          # LLM instructions in Markdown
```

- `domain/product.py` defines submitted product data, origin, business context,
  and the existing product-resolution contract.
- `domain/validation.py` defines the validation request and the `Validation` base
  class. The base class wraps each business result in an execution.
- `domain/validation_results.py` owns results, executions, and reports.
- `adapters/persistence/postgresql/` implements PostgreSQL storage; `alembic/` contains schema migrations.
- `domain/criterion.py` defines criterion results and YES/NO/UNKNOWN answers.
- `domain/validations/accessories/leasability/criteria.py` contains the concrete
  criterion classes used by accessory leasability.
- `domain/validation_service.py` runs the supplied validations and aggregates
  their results. Business rejection does not stop the suite; technical failure does.
- `domain/validations/accessories/suite.py` defines which accessory validations
  run and constructs the suite. Web dependencies obtain the service from here.
- Each concrete validation owns its business flow. Leasability selects the
  standard or BAWU async function from the order context. Both use ordinary
  conditionals and directly instantiate the criteria they evaluate; BAWU skips
  the StVZO acceptance check.
- Validation executions retain the submitted product and final business result.

Add new accessory checks under `domain/validations/accessories` and include them
in `suite.py`. A small check can be a single module; use a package when it needs
multiple files. Add another product category only when its validations are needed.
Domain code must not depend on FastAPI or concrete provider clients; adapters and
application assembly connect external implementations to business behavior.
