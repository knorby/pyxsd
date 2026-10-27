"""Fixed-issuer JWKS bearer verification with bounded refresh and pinned algorithms."""

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any

import anyio
import jwt
from mcp.server.auth.provider import AccessToken

from .errors import ToolError


class ConfiguredTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        fetcher: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = time.time,
        refresh_seconds: int = 300,
        max_jwks_bytes: int = 2**20,
    ) -> None:
        if (
            not audience
            or not self._secure_url(issuer)
            or not self._secure_url(jwks_url)
            or not algorithms
            or any(value not in {"RS256", "ES256", "EdDSA"} for value in algorithms)
            or type(refresh_seconds) is not int
            or refresh_seconds <= 0
            or type(max_jwks_bytes) is not int
            or max_jwks_bytes <= 0
        ):
            raise ToolError("invalid_argument", "Invalid fixed-issuer JWT configuration")
        self.issuer, self.audience, self.jwks_url = issuer, audience, jwks_url
        self.algorithms = tuple(dict.fromkeys(algorithms))
        self.fetcher = self._fetch if fetcher is None else fetcher
        self.clock = clock
        self.refresh_seconds, self.max_jwks_bytes = refresh_seconds, max_jwks_bytes
        self._keys: dict[str, Any] = {}
        self._loaded_at = 0.0
        self._lock = anyio.Lock()

    @staticmethod
    def _secure_url(value: str) -> bool:
        return isinstance(value, str) and (
            value.startswith("https://")
            or value.startswith("http://localhost")
            or value.startswith("http://127.0.0.1")
        )

    def _fetch(self, url: str) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.geturl() != url:
                    raise ToolError("auth_unavailable", "JWKS endpoint redirected")
                content = response.read(self.max_jwks_bytes + 1)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError("auth_unavailable", "JWKS fetch failed") from exc
        if len(content) > self.max_jwks_bytes:
            raise ToolError("auth_unavailable", "JWKS response exceeds its limit")
        return content

    def _load(self) -> None:
        try:
            document = json.loads(self.fetcher(self.jwks_url))
            keys = jwt.PyJWKSet.from_dict(document).keys
            self._keys = {key.key_id: key.key for key in keys if key.key_id}
            self._loaded_at = self.clock()
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError("auth_unavailable", "JWKS document is invalid") from exc
        if not self._keys:
            raise ToolError("auth_unavailable", "JWKS contains no usable keys")

    async def preflight(self) -> None:
        async with self._lock:
            self._load()

    async def _get_key(self, kid: str) -> Any:
        async with self._lock:
            if not self._keys or self.clock() - self._loaded_at >= self.refresh_seconds:
                self._load()
            if kid in self._keys:
                return self._keys[kid]
            # Unknown/rotated keys get one forced refresh per verification, not a loop.
            self._load()
            return self._keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            return None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self.algorithms or not isinstance(header.get("kid"), str):
                return None
            key = await self._get_key(header["kid"])
            if key is None:
                return None
            payload = jwt.decode(
                token,
                key=key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iss", "aud", "sub"],
                    "verify_exp": False,
                    "verify_signature": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
            expiry = payload["exp"]
            if type(expiry) not in (int, float) or self.clock() >= expiry:
                return None
            subject = payload["sub"]
            if not isinstance(subject, str) or not subject:
                return None
            scope = payload.get("scope", "")
            scopes = scope.split() if isinstance(scope, str) else None
            if scopes is None:
                return None
            resource = payload.get("aud")
            if isinstance(resource, list):
                resource = next((item for item in resource if item == self.audience), None)
            return AccessToken(
                token=token,
                client_id=str(payload.get("azp", payload.get("client_id", subject))),
                scopes=scopes,
                expires_at=int(expiry),
                resource=resource if isinstance(resource, str) else None,
                subject=subject,
                claims=payload,
            )
        except (jwt.PyJWTError, ToolError, ValueError, TypeError, KeyError, UnicodeError):
            return None
