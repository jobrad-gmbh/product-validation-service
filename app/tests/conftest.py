import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters.web.auth import TEST_VALIDATE_ROLE, VALIDATE_ROLE, JWTVerifier
from app.config.settings import Settings


@pytest.fixture(scope="session")
def jwks_server():
    """Serve real RSA public keys; no JWT decoder or auth dependency overrides."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk.update(kid="first", alg="RS256", use="sig")
    context = SimpleNamespace(
        key=key,
        keys=[public_jwk],
        requests=0,
        discovery_requests=0,
        fail=False,
        discovery_override=None,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            discovery = (
                self.path == "/accessory-validator/.well-known/openid-configuration"
            )
            if discovery:
                context.discovery_requests += 1
            else:
                context.requests += 1
            if context.fail:
                self.send_error(503)
                return
            if discovery:
                payload = context.discovery_override
                if payload is None:
                    payload = {
                        "issuer": context.issuer,
                        "jwks_uri": f"{context.issuer}/keys/signing",
                    }
            elif self.path.split("?", 1)[0] == "/accessory-validator/keys/signing":
                payload = {"keys": context.keys}
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context.issuer = f"http://127.0.0.1:{server.server_port}/accessory-validator"
    yield context
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def auth_context(jwks_server):
    settings = Settings(
        _env_file=None,
        APP_ENVIRONMENT="test",
        AUTH_ISSUER=jwks_server.issuer,
        AUTH_AUDIENCE="accessory-validator",
    )

    def mint(
        *,
        claims=None,
        roles=(VALIDATE_ROLE, TEST_VALIDATE_ROLE),
        key=None,
        kid="first",
        algorithm="RS256",
        omit=(),
    ):
        payload = {
            "iss": jwks_server.issuer,
            "aud": "accessory-validator",
            "sub": "odoo-service-account",
            "typ": "Bearer",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "realm_access": {"roles": list(roles)},
        }
        payload.update(claims or {})
        for name in omit:
            payload.pop(name, None)
        return jwt.encode(
            payload, key or jwks_server.key, algorithm=algorithm, headers={"kid": kid}
        )

    return SimpleNamespace(verifier=JWTVerifier(settings), mint=mint, settings=settings)
