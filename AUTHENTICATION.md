# Service authentication

Both validation endpoints require an OAuth 2.0 access token in the
`Authorization: Bearer <token>` header. `/health` and the API documentation are
public.

| Endpoint | Required role |
| --- | --- |
| `POST /api/v1/accessories/validate` | `SR_accessory-validator_validate` |
| `POST /api/v1/accessories/validate/test` | `SR_accessory-validator_test_validate` |

The roles are independent: a token allowed to validate cannot run test
validations, and vice versa. A missing, invalid, or expired token returns `401`;
a valid token without the role returns `403`. If the signing keys cannot be
fetched from the provider, the API returns `503` and authorizes nothing.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_ISSUER` | — (required to start the API) | Exact issuer URL, e.g. `https://iam.example/realms/internal` |
| `AUTH_AUDIENCE` | `accessory-validator` | Value tokens must carry in `aud` |
| `AUTH_JWKS_TIMEOUT_SECONDS` | `5` | Timeout for discovery and signing-key requests |

`AUTH_ISSUER` must match the token's `iss` claim and the `issuer` in
`<AUTH_ISSUER>/.well-known/openid-configuration` character for character,
including any trailing slash. The signing-key URL is read from that discovery
document. Database-only commands such as `alembic upgrade head` do not need
`AUTH_ISSUER`.

## Accepted tokens

A token is accepted when it:

- is signed with RS256 by a key published by the issuer, and has a `kid` header;
- has `iss` equal to `AUTH_ISSUER` and `aud` containing `AUTH_AUDIENCE`;
- has not expired (30 seconds of clock skew are tolerated) and has a non-empty `sub`;
- has `typ` equal to `Bearer`, which Keycloak sets only on access tokens. ID and
  refresh tokens are rejected.

Roles are read from:

- `realm_access.roles` or a top-level `roles` claim, using the full
  `SR_accessory-validator_*` names;
- `resource_access.<AUTH_AUDIENCE>.roles`, where the short names `validate` and
  `test_validate` are also accepted. Roles under other clients are ignored.

## Local tokens

The `local-auth` Compose profile runs a mock OAuth server with three clients
(see `local/oauth-config.json`):

| `client_id` | Granted role |
| --- | --- |
| `odoo-accessory-validator` | validate |
| `accessory-validator-tester` | test validate |
| anything else | none (useful for checking `403`) |

Start it and request tokens with the client-credentials grant. The mock server
accepts any client secret.

```bash
docker compose --profile local-auth up -d oauth

ACCESS_TOKEN=$(curl -s http://localhost:9010/accessory-validator/token \
  -d grant_type=client_credentials \
  -d client_id=odoo-accessory-validator -d client_secret=local \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')

TEST_ACCESS_TOKEN=$(curl -s http://localhost:9010/accessory-validator/token \
  -d grant_type=client_credentials \
  -d client_id=accessory-validator-tester -d client_secret=local \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
```

Tokens are valid for one hour. The mock server derives the issuer from the
request's host, so the token must be requested through the same host as
`AUTH_ISSUER`:

- API on your computer (`AUTH_ISSUER=http://localhost:9010/accessory-validator`):
  use the commands above as they are.
- API in Docker (`AUTH_ISSUER=http://oauth:9010/accessory-validator`): add
  `-H 'Host: oauth:9010'` to each `curl` command.

## Deployed environments

Register the calling service as a confidential client in IAM and configure it
so that its access tokens:

1. include `accessory-validator` in `aud` (in Keycloak, add an audience mapper
   to the client or its client scope);
2. carry `SR_accessory-validator_validate` and/or
   `SR_accessory-validator_test_validate`, as realm roles or as the `validate`
   and `test_validate` client roles of the `accessory-validator` client.

Then set `AUTH_ISSUER` to the realm's issuer URL as published in its discovery
document.
