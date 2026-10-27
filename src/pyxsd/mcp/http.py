"""Authenticated streamable HTTP and bounded binary transfer."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import (
    RequestBodyLimitMiddleware,
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from pydantic import AnyHttpUrl

from .config import Settings
from .errors import ToolError
from .safe_io import AdmittedFile, validate_paths
from .server import Services, create_server


class _TransportSecurity:
    def __init__(self, app: ASGIApp, security: TransportSecuritySettings) -> None:
        self.app = app
        self.guard = TransportSecurityMiddleware(settings=security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            error = await self.guard.validate_request(
                request, is_post=scope.get("method") == "POST"
            )
            if error is not None:
                return await error(scope, receive, send)
        await self.app(scope, receive, send)


def _fault(exc: ToolError) -> JSONResponse:
    status = {
        "forbidden": 403,
        "not_found": 404,
        "quota_exceeded": 413,
        "stale_reference": 409,
        "source_changed": 409,
        "invalid_argument": 400,
        "invalid_path": 400,
        "invalid_encoding": 400,
    }.get(exc.fault.code, 400)
    return JSONResponse(
        {"status": "error", "fault": exc.fault.model_dump(mode="json")}, status_code=status
    )


def _integer(value: str | None, default: int, *, maximum: int | None = None) -> int:
    if value is None:
        return default
    if not value.isdigit():
        raise ToolError("invalid_argument", "Artifact range must be an integer")
    result = int(value)
    if maximum is not None and result > maximum:
        raise ToolError("invalid_argument", "Artifact range exceeds its budget")
    return result


async def _put_files(request: Request, services: Services) -> Response:
    try:
        principal = services.current_principal()
        workspace = request.path_params["workspace"]
        path = request.path_params["path"]
        validate_paths([path])
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > services.settings.limits.request_bytes:
                raise ToolError("quota_exceeded", "HTTP request exceeds its byte budget")
            chunks.append(chunk)
        snapshot = await services.snapshots.put_bytes(
            principal,
            workspace,
            [AdmittedFile(path, b"".join(chunks))],
            expected_revision=request.headers.get("x-pyxsd-expected-revision"),
        )
        return JSONResponse(
            {
                "workspace_id": snapshot.workspace_id,
                "revision": snapshot.revision,
                "files": [
                    {"path": item.path, "digest": item.digest, "size": item.size}
                    for item in snapshot.files
                ],
            },
            status_code=201,
        )
    except ToolError as exc:
        return _fault(exc)


async def _get_artifact(request: Request, services: Services) -> Response:
    try:
        identifier = request.path_params["artifact_id"]
        offset = _integer(request.query_params.get("offset"), 0)
        length = _integer(
            request.query_params.get("length"),
            services.settings.limits.result_bytes,
            maximum=services.settings.limits.result_bytes,
        )
        if length <= 0:
            raise ToolError("invalid_argument", "Artifact range length must be positive")
        lease = services.artifacts.lease(services.current_principal(), identifier)
        reader = await lease.__aenter__()
        ref = reader.ref
        end = min(ref.size, offset + length)
    except ToolError as exc:
        return _fault(exc)

    async def content() -> AsyncIterator[bytes]:
        try:
            for position in range(offset, end, services.settings.limits.result_bytes):
                yield await reader.read(
                    offset=position,
                    length=min(services.settings.limits.result_bytes, end - position),
                )
        finally:
            await lease.__aexit__(None, None, None)

    return StreamingResponse(
        content(),
        media_type=ref.media_type,
        headers={
            "Content-Length": str(max(0, end - offset)),
            "X-Content-SHA256": ref.digest,
            "Cache-Control": "private, no-store",
        },
    )


def create_http_app(
    settings: Settings, services: Services, verifier: TokenVerifier | None
) -> Starlette:
    if settings.http is None:
        raise ToolError("auth_required", "HTTP issuer and JWKS configuration are required")
    if verifier is None and not settings.http.loopback_dev:
        raise ToolError("auth_required", "HTTP requires a token verifier or explicit loopback dev")
    if settings.execution_backend == "trusted-local":
        raise ToolError("execution_unavailable", "Trusted-local execution is not allowed over HTTP")
    if settings.http.loopback_dev and any(
        host.split(":", 1)[0] not in {"127.0.0.1", "localhost"}
        for host in settings.http.allowed_hosts
    ):
        raise ToolError("invalid_argument", "Loopback development requires loopback hosts")

    issuer = AnyHttpUrl(settings.http.issuer_url)
    audience = AnyHttpUrl(settings.http.audience)
    auth = None
    if verifier is not None:
        auth = AuthSettings(
            issuer_url=issuer,
            resource_server_url=audience,
            required_scopes=["pyxsd:read"],
            validate_token_resource=True,
        )
    server = create_server(settings, services, token_verifier=verifier, auth=auth)
    security = TransportSecuritySettings(
        allowed_hosts=sorted(settings.http.allowed_hosts),
        allowed_origins=sorted(settings.http.allowed_origins),
    )
    metadata_url = build_resource_metadata_url(audience)

    def protected(endpoint: Callable[[Request], Awaitable[Response]], scopes: list[str]) -> Any:
        converted = request_response(endpoint)
        if verifier is None:
            return converted
        return RequireAuthMiddleware(converted, scopes, metadata_url)

    mcp_application = server.streamable_http_app(
        json_response=True,
        max_request_body_size=settings.limits.request_bytes,
        transport_security=security,
    )

    async def put_endpoint(request: Request) -> Response:
        return await _put_files(request, services)

    async def get_endpoint(request: Request) -> Response:
        return await _get_artifact(request, services)

    routes = [
        Route(
            "/files/{workspace}/{path:path}",
            endpoint=protected(put_endpoint, ["pyxsd:upload"]),
            methods=["PUT"],
        ),
        Route(
            "/artifacts/{artifact_id}",
            endpoint=protected(get_endpoint, ["pyxsd:read"]),
            methods=["GET"],
        ),
        Mount("/", app=mcp_application),
    ]
    middleware = [
        Middleware(_TransportSecurity, security=security),
        Middleware(RequestBodyLimitMiddleware, max_body_size=settings.limits.request_bytes),
    ]
    if verifier is not None:
        middleware.extend(
            [
                Middleware(
                    AuthenticationMiddleware,
                    backend=BearerAuthBackend(verifier, resource_server_url=audience),
                ),
                Middleware(AuthContextMiddleware),
            ]
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            preflight = getattr(verifier, "preflight", None)
            if preflight is not None:
                await preflight()
            async with server.session_manager.run():
                yield
        finally:
            await services.aclose()

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
