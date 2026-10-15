import json
import time
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.adapters.web import auth
from app.adapters.web.auth import TEST_VALIDATE_ROLE, VALIDATE_ROLE, JWTVerifier
from app.adapters.web.dependencies import get_validation_service
from app.adapters.web.system_routes import system_router
from app.adapters.web.validation_routes import validation_router
from app.config.settings import Settings

VALIDATE_PATH = "/api/v1/accessories/validate"
TEST_PATH = "/api/v1/accessories/validate/test"
PAYLOAD = {
    "brand": "Example",
    "model": "Rack",
    "price": "49.99",
    "origin": {"source": "odoo", "external_ref": "42"},
}


@pytest.fixture
def secured_api(auth_context, monkeypatch):
    app = FastAPI()
    app.state.jwt_verifier = auth_context.verifier
    app.include_router(system_router)
    app.include_router(validation_router, prefix="/api/v1")
    service = AsyncMock()
    service.validate.side_effect = HTTPException(
        status_code=418, detail="Reached validation"
    )
    app.dependency_overrides[get_validation_service] = lambda: service
    build_test = Mock(return_value=service)
    monkeypatch.setattr(
        "app.adapters.web.validation_routes.build_testing_validation_service",
        build_test,
    )
    with TestClient(app) as client:
        yield client, service, build_test


def call(client, path, token=None):
    payload = (
        PAYLOAD
        if path == VALIDATE_PATH
        else {**PAYLOAD, "llm_settings": {"api_key": "llm-only-key"}}
    )
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post(path, json=payload, headers=headers)


@pytest.mark.parametrize("path", [VALIDATE_PATH, TEST_PATH])
@pytest.mark.parametrize(
    "authorization", [None, "Basic abc", "Bearer", "Bearer garbage"]
)
def test_requires_bearer_before_validation(secured_api, path, authorization):
    client, service, build_test = secured_api
    headers = {"Authorization": authorization} if authorization is not None else {}
    response = client.post(path, json=PAYLOAD, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    service.validate.assert_not_awaited()
    build_test.assert_not_called()


@pytest.mark.parametrize(
    "claims,omit",
    [
        ({"exp": int(time.time()) - 60}, ()),
        ({"nbf": int(time.time()) + 3600}, ()),
        ({"iss": "https://untrusted.example/realm"}, ()),
        ({"aud": "other-api"}, ()),
        ({"sub": ""}, ()),
        ({"sub": 123}, ()),
        ({}, ("exp",)),
        ({}, ("iss",)),
        ({}, ("aud",)),
        ({}, ("sub",)),
        ({"typ": "ID"}, ()),
        ({"typ": "Refresh"}, ()),
        ({}, ("typ",)),
    ],
)
def test_rejects_invalid_claims(secured_api, auth_context, claims, omit):
    client, service, _ = secured_api
    token = auth_context.mint(claims=claims, omit=omit)
    response = call(client, VALIDATE_PATH, token)
    assert response.status_code == 401
    assert token not in response.text
    service.validate.assert_not_awaited()


@pytest.mark.parametrize("algorithm", ["RS512", "PS256", "HS256", "none"])
def test_pins_rs256(secured_api, auth_context, algorithm):
    key = "test-hmac-secret-with-at-least-32-bytes" if algorithm == "HS256" else None
    if algorithm == "none":
        token = jwt.encode(
            {"sub": "forged"}, None, algorithm="none", headers={"kid": "first"}
        )
    else:
        token = auth_context.mint(algorithm=algorithm, key=key)
    response = call(secured_api[0], VALIDATE_PATH, token)
    assert response.status_code == 401
    secured_api[1].validate.assert_not_awaited()


def test_rejects_wrong_signature(secured_api, auth_context):
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = call(secured_api[0], VALIDATE_PATH, auth_context.mint(key=rogue))
    assert response.status_code == 401
    secured_api[1].validate.assert_not_awaited()


@pytest.mark.parametrize(
    "roles,path,allowed",
    [
        ((VALIDATE_ROLE,), VALIDATE_PATH, True),
        ((VALIDATE_ROLE,), TEST_PATH, False),
        ((TEST_VALIDATE_ROLE,), TEST_PATH, True),
        ((TEST_VALIDATE_ROLE,), VALIDATE_PATH, False),
        ((), VALIDATE_PATH, False),
        ((), TEST_PATH, False),
    ],
)
def test_endpoint_permissions_are_independent(
    secured_api, auth_context, roles, path, allowed
):
    client, service, _ = secured_api
    response = call(client, path, auth_context.mint(roles=roles))
    assert response.status_code == (418 if allowed else 403)
    assert service.validate.await_count == int(allowed)


@pytest.mark.parametrize(
    "claims,allowed",
    [
        ({"resource_access": {"accessory-validator": {"roles": ["validate"]}}}, True),
        (
            {
                "resource_access": {
                    "foreign-api": {"roles": ["validate", VALIDATE_ROLE]}
                }
            },
            False,
        ),
        ({"roles": [VALIDATE_ROLE]}, True),
        ({"roles": ["validate"]}, False),
        ({"realm_access": {"roles": "validate"}}, False),
        (
            {
                "realm_access": [],
                "resource_access": {"accessory-validator": {"roles": [12]}},
            },
            False,
        ),
    ],
)
def test_scopes_client_privileges_and_handles_malformed_roles(
    secured_api, auth_context, claims, allowed
):
    response = call(
        secured_api[0], VALIDATE_PATH, auth_context.mint(roles=(), claims=claims)
    )
    assert response.status_code == (418 if allowed else 403)


def test_jwks_cache_and_rotation(auth_context, jwks_server, monkeypatch):
    initial = jwks_server.requests
    initial_discovery = jwks_server.discovery_requests
    auth_context.verifier.verify(auth_context.mint())
    auth_context.verifier.verify(auth_context.mint())
    assert jwks_server.requests == initial + 1
    assert jwks_server.discovery_requests == initial_discovery + 1
    assert auth_context.verifier.jwks.uri == f"{jwks_server.issuer}/keys/signing"
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rotated.public_key()))
    jwk.update(kid="rotated", alg="RS256", use="sig")
    original_keys = jwks_server.keys
    jwks_server.keys = [jwk]
    try:
        monkeypatch.setattr(
            auth_context.verifier.jwks, "_last_successful_fetch", time.monotonic() - 31
        )
        assert (
            auth_context.verifier.verify(
                auth_context.mint(key=rotated, kid="rotated")
            ).subject
            == "odoo-service-account"
        )
        assert jwks_server.requests == initial + 2
    finally:
        jwks_server.keys = original_keys


def test_unknown_key_is_401(secured_api, auth_context):
    assert (
        call(
            secured_api[0], VALIDATE_PATH, auth_context.mint(kid="missing")
        ).status_code
        == 401
    )


def test_jwks_failure_is_503_and_does_not_authorize(
    secured_api, auth_context, jwks_server
):
    # Discover successfully first, then fail fetching the actual key set.
    _ = auth_context.verifier.jwks
    jwks_server.fail = True
    try:
        response = call(secured_api[0], VALIDATE_PATH, auth_context.mint())
        assert response.status_code == 503
        secured_api[1].validate.assert_not_awaited()
    finally:
        jwks_server.fail = False


def test_health_is_public_and_openapi_advertises_bearer(secured_api):
    client, *_ = secured_api
    assert client.get("/health").status_code == 200
    document = client.get("/openapi.json").json()
    for path in (VALIDATE_PATH, TEST_PATH):
        assert document["paths"][path]["post"]["security"] == [{"HTTPBearer": []}]


@pytest.mark.parametrize(
    "environment",
    ["development", "test", "integration", "staging", "production", "unknown"],
)
def test_configured_provider_works_in_every_environment(auth_context, environment):
    settings = auth_context.settings.model_copy(update={"APP_ENVIRONMENT": environment})
    verifier = JWTVerifier(settings)
    assert verifier.verify(auth_context.mint()).subject == "odoo-service-account"


@pytest.mark.parametrize(
    "issuer",
    [
        "file:///realm",
        "https://iam.example:invalid/realm",
    ],
)
def test_auth_requires_explicit_valid_issuer(issuer):
    with pytest.raises(ValueError):
        Settings(_env_file=None, AUTH_ISSUER=issuer)


@pytest.mark.parametrize("issuer", [None, ""])
def test_settings_load_without_issuer_but_api_refuses_to_start(issuer):
    settings = Settings(_env_file=None, AUTH_ISSUER=issuer)
    assert settings.AUTH_ISSUER is None
    with pytest.raises(RuntimeError, match="AUTH_ISSUER"):
        JWTVerifier(settings)


@pytest.mark.parametrize(
    "issuer",
    ["https://IAM.example", "HTTPS://iam.example/realms/x", "https://iam.example/x/"],
)
def test_issuer_is_kept_verbatim(issuer):
    assert Settings(_env_file=None, AUTH_ISSUER=issuer).AUTH_ISSUER == issuer


@pytest.mark.parametrize(
    "issuer",
    [
        "https://iam.example/realms/internal",
        "http://auth.example/tenant",
    ],
)
def test_only_issuer_url_is_required_and_discovery_is_lazy(issuer):
    settings = Settings(_env_file=None, AUTH_ISSUER=issuer)
    verifier = JWTVerifier(settings)
    assert verifier.issuer == issuer
    assert verifier._jwks is None
    assert "AUTH_JWKS_URL" not in Settings.model_fields


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {},
        {
            "issuer": "https://foreign.example/realm",
            "jwks_uri": "https://foreign.example/jwks",
        },
        {"jwks_uri": "https://keys.example/jwks"},
        {"issuer": "MATCH"},
        {"issuer": "MATCH", "jwks_uri": 123},
        {"issuer": "MATCH", "jwks_uri": "file:///keys"},
        {"issuer": "MATCH", "jwks_uri": "https://keys.example:invalid/jwks"},
    ],
)
def test_rejects_invalid_discovery_before_fetching_keys(
    secured_api, auth_context, jwks_server, metadata
):
    if isinstance(metadata, dict):
        metadata = {**metadata}
        if metadata.get("issuer") == "MATCH":
            metadata["issuer"] = jwks_server.issuer
    jwks_server.discovery_override = metadata
    initial = jwks_server.requests
    try:
        response = call(secured_api[0], VALIDATE_PATH, auth_context.mint())
        assert response.status_code == 503
        assert jwks_server.requests == initial
        secured_api[1].validate.assert_not_awaited()
    finally:
        jwks_server.discovery_override = None


def test_discovery_failure_is_retryable(
    secured_api, auth_context, jwks_server, monkeypatch
):
    monkeypatch.setattr(auth, "DISCOVERY_RETRY_SECONDS", 0)
    jwks_server.fail = True
    try:
        assert (
            call(secured_api[0], VALIDATE_PATH, auth_context.mint()).status_code == 503
        )
        secured_api[1].validate.assert_not_awaited()
    finally:
        jwks_server.fail = False
    assert call(secured_api[0], VALIDATE_PATH, auth_context.mint()).status_code == 418


def test_discovered_signing_key_url_allows_query_parameters(auth_context, jwks_server):
    uri = f"{jwks_server.issuer}/keys/signing?version=2"
    jwks_server.discovery_override = {"issuer": jwks_server.issuer, "jwks_uri": uri}
    try:
        verifier = JWTVerifier(auth_context.settings)
        assert verifier.verify(auth_context.mint()).subject == "odoo-service-account"
        assert verifier.jwks.uri == uri
    finally:
        jwks_server.discovery_override = None


@pytest.mark.parametrize(
    "issuer",
    [
        "https://user:secret@iam.example/realm",
        "https://iam.example/realm?issuer=x",
        "https://iam.example/realm#fragment",
    ],
)
def test_issuer_url_components_are_left_to_provider_discovery(issuer):
    settings = Settings(_env_file=None, AUTH_ISSUER=issuer)
    verifier = JWTVerifier(settings)
    assert verifier.issuer == issuer
    assert verifier._jwks is None


def test_failed_discovery_is_not_repeated_during_backoff(
    secured_api, auth_context, jwks_server, monkeypatch
):
    jwks_server.fail = True
    initial = jwks_server.discovery_requests
    try:
        for _ in range(3):
            response = call(secured_api[0], VALIDATE_PATH, auth_context.mint())
            assert response.status_code == 503
        assert jwks_server.discovery_requests == initial + 1
    finally:
        jwks_server.fail = False
    monkeypatch.setattr(
        auth_context.verifier,
        "_discovery_failed_at",
        auth_context.verifier._discovery_failed_at - auth.DISCOVERY_RETRY_SECONDS,
    )
    assert call(secured_api[0], VALIDATE_PATH, auth_context.mint()).status_code == 418


def test_tolerates_small_clock_skew(auth_context):
    now = int(time.time())
    token = auth_context.mint(claims={"iat": now + 10, "nbf": now + 10})
    assert auth_context.verifier.verify(token).subject == "odoo-service-account"
