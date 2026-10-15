"""Verify service bearer tokens and enforce JR IAM endpoint permissions."""

import time
from dataclasses import dataclass
from threading import Lock
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from pydantic import HttpUrl

from app.config.settings import Settings

VALIDATE_ROLE = "SR_product-validator-service__accessory_validate"
TEST_VALIDATE_ROLE = "SR_product-validator-service_accessory_test_validate"
PRIVILEGES = {VALIDATE_ROLE: "validate", TEST_VALIDATE_ROLE: "test_validate"}
# Tolerates small clock differences between the IAM host and this service.
CLOCK_SKEW_LEEWAY_SECONDS = 30
# After a failed discovery, answer 503 immediately instead of queueing every
# request behind another slow attempt while the provider is unreachable.
DISCOVERY_RETRY_SECONDS = 5
bearer = HTTPBearer(auto_error=False, description="Service access token from Keycloak")


@dataclass(frozen=True)
class AuthenticatedClient:
    subject: str
    roles: frozenset[str]


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing, invalid, or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def auth_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service unavailable",
    )


def string_roles(value: Any) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return set()
    return set(value)


def service_roles(payload: dict[str, Any], audience: str) -> frozenset[str]:
    # SR_* realm roles are globally named by IAM. Bare privilege names belong to
    # a client: only resource_access for THIS API can grant validate/test_validate.
    realm_access = payload.get("realm_access")
    realm_roles = (
        string_roles(realm_access.get("roles"))
        if isinstance(realm_access, dict)
        else set()
    )
    roles = realm_roles | string_roles(payload.get("roles"))
    resource_access = payload.get("resource_access")
    own_client = (
        resource_access.get(audience) if isinstance(resource_access, dict) else None
    )
    own_roles = (
        string_roles(own_client.get("roles")) if isinstance(own_client, dict) else set()
    )
    roles |= own_roles
    for role, privilege in PRIVILEGES.items():
        if privilege in own_roles:
            roles.add(role)
    return frozenset(roles & PRIVILEGES.keys())


class JWTVerifier:
    def __init__(self, settings: Settings) -> None:
        if settings.AUTH_ISSUER is None:
            raise RuntimeError("AUTH_ISSUER must be set to start the API")
        self.issuer = settings.AUTH_ISSUER
        self.audience = settings.AUTH_AUDIENCE
        self.timeout = settings.AUTH_JWKS_TIMEOUT_SECONDS
        self._jwks: PyJWKClient | None = None
        self._discovery_failed_at: float | None = None
        self._discovery_lock = Lock()

    @property
    def jwks(self) -> PyJWKClient:
        # Discover once, on first verification. Failed discovery remains retryable.
        # The lock avoids concurrent requests constructing separate key caches.
        with self._discovery_lock:
            if self._jwks is None:
                if (
                    self._discovery_failed_at is not None
                    and time.monotonic() - self._discovery_failed_at
                    < DISCOVERY_RETRY_SECONDS
                ):
                    raise auth_unavailable()
                try:
                    uri = self._discover_jwks_uri()
                except (httpx.HTTPError, ValueError):
                    self._discovery_failed_at = time.monotonic()
                    raise auth_unavailable() from None
                self._discovery_failed_at = None
                # The trusted provider supplies the URL; token claims never do.
                self._jwks = PyJWKClient(uri, timeout=self.timeout)
            return self._jwks

    def _discover_jwks_uri(self) -> str:
        response = httpx.get(
            f"{self.issuer.rstrip('/')}/.well-known/openid-configuration",
            timeout=self.timeout,
            follow_redirects=False,
        )
        response.raise_for_status()
        metadata = response.json()
        if not isinstance(metadata, dict) or metadata.get("issuer") != self.issuer:
            raise ValueError("Discovery issuer does not match the configured issuer")
        uri = metadata.get("jwks_uri")
        if not isinstance(uri, str):
            raise ValueError("Discovery is missing jwks_uri")
        HttpUrl(uri)
        return uri

    def verify(self, token: str) -> AuthenticatedClient:
        try:
            if len(token) > 16384:
                raise unauthorized()
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
            ):
                raise unauthorized()
            signing_key = self.jwks.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            # Keycloak signs ID and refresh tokens with the same realm key; only
            # access tokens carry typ "Bearer".
            if payload.get("typ") != "Bearer":
                raise unauthorized()
            if not isinstance(payload["sub"], str) or not payload["sub"].strip():
                raise unauthorized()
        except PyJWKClientConnectionError:
            raise auth_unavailable() from None
        except (jwt.InvalidTokenError, PyJWKClientError):
            raise unauthorized() from None
        return AuthenticatedClient(
            payload["sub"], service_roles(payload, self.audience)
        )


def authenticate_service(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedClient:
    # A sync dependency runs in FastAPI's thread pool; JWKS I/O does not block
    # the event loop used for concurrent validations.
    if credentials is None:
        raise unauthorized()
    verifier: JWTVerifier = request.app.state.jwt_verifier
    return verifier.verify(credentials.credentials)


def require_validate(
    client: Annotated[AuthenticatedClient, Depends(authenticate_service)],
) -> None:
    if VALIDATE_ROLE not in client.roles:
        raise HTTPException(status_code=403, detail="Missing validate permission")


def require_test_validate(
    client: Annotated[AuthenticatedClient, Depends(authenticate_service)],
) -> None:
    if TEST_VALIDATE_ROLE not in client.roles:
        raise HTTPException(status_code=403, detail="Missing test validate permission")
