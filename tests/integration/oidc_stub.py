"""测试用最小 OIDC AS（Authorization Code + PKCE + RS256 id_token）。"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64url_uint(n: int) -> str:
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


class OidcStub:
    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = self.private.public_key()
        self._codes: dict[str, dict] = {}
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> str:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/.well-known/openid-configuration":
                    body = {
                        "issuer": stub.base_url,
                        "authorization_endpoint": f"{stub.base_url}/authorize",
                        "token_endpoint": f"{stub.base_url}/token",
                        "jwks_uri": f"{stub.base_url}/jwks",
                    }
                    self._json(200, body)
                    return
                if parsed.path == "/jwks":
                    numbers = stub.public.public_numbers()
                    self._json(
                        200,
                        {
                            "keys": [
                                {
                                    "kty": "RSA",
                                    "kid": "test",
                                    "use": "sig",
                                    "alg": "RS256",
                                    "n": _b64url_uint(numbers.n),
                                    "e": _b64url_uint(numbers.e),
                                }
                            ]
                        },
                    )
                    return
                if parsed.path == "/authorize":
                    qs = parse_qs(parsed.query)
                    code = "test-code-" + qs["state"][0][:8]
                    stub._codes[code] = {
                        "nonce": qs["nonce"][0],
                        "code_challenge": qs["code_challenge"][0],
                        "redirect_uri": qs["redirect_uri"][0],
                    }
                    loc = (
                        f"{qs['redirect_uri'][0]}?code={code}&state={qs['state'][0]}"
                    )
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return
                self.send_error(404)

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path != "/token":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode()
                form = parse_qs(raw)
                code = form.get("code", [None])[0]
                verifier = form.get("code_verifier", [None])[0]
                if code not in stub._codes:
                    self._json(400, {"error": "invalid_grant"})
                    return
                pending = stub._codes.pop(code)
                digest = hashlib.sha256(verifier.encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if challenge != pending["code_challenge"]:
                    self._json(400, {"error": "invalid_grant"})
                    return
                now = datetime.now(UTC)
                id_token = jwt.encode(
                    {
                        "iss": stub.base_url,
                        "aud": stub.client_id,
                        "sub": "oidc-user-1",
                        "nonce": pending["nonce"],
                        "iat": now,
                        "exp": now + timedelta(minutes=5),
                        "roles": ["operator", "viewer"],
                        "project_ids": [],
                    },
                    stub.private,
                    algorithm="RS256",
                    headers={"kid": "test"},
                )
                self._json(
                    200,
                    {
                        "access_token": "access",
                        "token_type": "Bearer",
                        "id_token": id_token,
                    },
                )

            def _json(self, status: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
