# Multi-Tenant Wallet API (Cashless AI take-home)

Django + DRF ledger service. Tenants (merchants/orgs) are fully isolated; every balance
change is an immutable transaction; transfers are atomic; money endpoints are idempotent
per tenant.

## Models

- `Tenant(id, name, api_key, created_at)` — resolved from `X-API-Key` (preferred) or `X-Tenant-ID`.
- `TenantUser(id, tenant FK, email unique-per-tenant, display_name)` — end user under one tenant.
- `Wallet(id, tenant FK, user OneToOne, balance minor-units BigInteger)` — one wallet per user
  (assumption, keeps assessment scope tight). `balance` is a lockable cache; `Transaction` rows are truth.
- `Transaction(id, tenant, wallet, tx_type, amount, balance_after, counterpart_wallet)` — append-only
  (`save()` blocks updates, no update/delete endpoints).
- `IdempotencyRecord(tenant, key unique-per-tenant, status_code, response)` — replay store.

## Endpoints (`/api/`)

| Method | Path | Auth | Body | Notes |
|---|---|---|---|---|
| POST | `/tenants/` | public | `{name}` | returns `{id, api_key}` (key shown once) |
| POST/GET | `/users/` `/users/<id>/` | tenant | `{email, display_name}` | email unique per tenant |
| POST/GET | `/wallets/` `/wallets/<id>/` | tenant | `{user_id}` | `user` must belong to requesting tenant |
| POST | `/wallets/<id>/deposit/` | tenant | `{amount, idempotency_key}` | amount = integer minor units, `>= 1` |
| POST | `/wallets/<id>/withdraw/` | tenant | `{amount, idempotency_key}` | `422` + `Insufficient funds` if short |
| POST | `/transfers/` | tenant | `{from_wallet, to_wallet, amount, idempotency_key}` | same-tenant only, atomic pair (`transfer_out` + `transfer_in`) |
| GET | `/wallets/<id>/transactions/` | tenant | — | paginated (`count/next/previous/results`) |

Cross-tenant object access returns `404` (no existence leak); cross-tenant transfer returns `400`.

## Setup

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py test tenantapp.wallets -v 2
uv run python manage.py runserver 8000
```

Environment (see `.env.example`): `DJANGO_SECRET_KEY` is required in production.
The code falls back to `dev-insecure-secret-key` only for local review — never
use the fallback in production. `DJANGO_DEBUG=1/0`, `DJANGO_ALLOWED_HOSTS`
(comma-separated), and `POSTGRES_DB/USER/PASSWORD/HOST/PORT` configure debug
mode, hosts, and Postgres selection. Without `POSTGRES_DB`, SQLite
`db.sqlite3` is used (already in `.gitignore`; do not commit it).

PostgreSQL (preferred for true `select_for_update` row locking):

```bash
docker compose up --build
# or locally:
POSTGRES_DB=tenantapp POSTGRES_USER=tenantapp POSTGRES_PASSWORD=tenantapp POSTGRES_HOST=localhost \
  uv run python manage.py migrate
```

## Example

```bash
# 1. tenant (public)
curl -X POST localhost:8000/api/tenants/ -H 'Content-Type: application/json' -d '{"name":"Acme"}'
# -> {"id":"...","api_key":"..."}  (save both as TENANT_ID / API_KEY)

# 2. user + wallet (scoped)
curl -X POST localhost:8000/api/users/ -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{"email":"a@acme.com"}'
curl -X POST localhost:8000/api/wallets/ -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{"user_id":"<user-id>"}'

# 3. money (idempotent)
curl -X POST localhost:8000/api/wallets/<id>/deposit/ -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{"amount":1000,"idempotency_key":"dep-1"}'
curl -X POST localhost:8000/api/transfers/ -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' -d '{"from_wallet":"<w1>","to_wallet":"<w2>","amount":400,"idempotency_key":"x-1"}'
# retry with same idempotency_key -> identical response, no double-charge

# 4. balance + history
curl localhost:8000/api/wallets/<id>/ -H "X-API-Key: $API_KEY"
curl localhost:8000/api/wallets/<id>/transactions/ -H "X-API-Key: $API_KEY"
```

`X-Tenant-ID: <tenant-uuid>` also works as a dev-convenience header where `X-API-Key` is expected.

## Correctness notes

- **Atomicity/locking:** money ops run in `transaction.atomic()` with `select_for_update()`;
  transfers lock both wallet rows in one query and validate funds before writing the paired txs.
- **Idempotency:** unique `(tenant, key)` on `IdempotencyRecord`; check runs inside
  `transaction.atomic()` with `select_for_update()` plus `IntegrityError` catch replays
  the winner under concurrent retries. `422` insufficient-funds and `400`
  cross-tenant responses are also recorded, so retries with the same key replay
  identically. Transfers store one record covering both legs.
- **Ledger truth:** every mutation writes `Transaction` rows with `balance_after`; wallet detail test
  recomputes last `balance_after == wallet.balance`.
- **Money:** `BigInteger` minor units + `MinValueValidator(1)`; never float.
- **SQLite vs Postgres:** default SQLite is fine for review; `select_for_update()` is a no-op there and
  table locks serialize writers, so `services.py` retries `OperationalError: database is locked`
  (8 attempts, linear backoff). Use Postgres/Docker for real concurrency guarantees.

## Assumptions / trade-offs

1. One wallet per user (`OneToOne`). Multi-wallet-per-user would just drop the constraint.
2. Shared-schema multi-tenancy (`tenant_id` column + mandatory scoping) over schema-per-tenant.
3. `X-Tenant-ID` accepted unauthenticated for assessment ease; production would require `X-API-Key` only.
4. Insufficient funds returns `422` (distinct from `400` validation) with current `balance` included.
