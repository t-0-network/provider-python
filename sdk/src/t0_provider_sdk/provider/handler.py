"""Generic handler registration for ConnectRPC ASGI/WSGI applications.

Go equivalent: provider/handler.go → Handler[T] + NewHttpHandler()

This module provides proto-agnostic handler registration. It works with
any generated ConnectRPC service application class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from t0_provider_sdk.provider.interceptor import SignatureErrorInterceptor
from t0_provider_sdk.provider.middleware import (
    DEFAULT_MAX_BODY_SIZE,
    ASGIApp,
    new_verify_signature,
    signature_verification_middleware,
)

T = TypeVar("T")

# Type for a function that creates an ASGI app from handler options
BuildHandler = Callable[["_HandlerOptions"], tuple[str, ASGIApp]]

# Type for handler option modifiers
HandlerOption = Callable[["_HandlerOptions"], None]


@dataclass
class _HandlerOptions:
    """Internal options passed to handler builders.

    Go equivalent: providerHandlerOptions
    """

    interceptors: list[Any] = field(default_factory=list)
    max_body_size: int = DEFAULT_MAX_BODY_SIZE


def handler(
    asgi_app_factory: type[Any],
    service_impl: Any,
    *options: HandlerOption,
) -> BuildHandler:
    """Register a service handler with optional configuration.

    Go equivalent: Handler[T any](factory, impl, opts...)

    Args:
        asgi_app_factory: Generated ConnectRPC ASGI application class
            (e.g. ProviderServiceASGIApplication).
        service_impl: User's implementation of the service protocol.
        *options: Optional handler configuration functions.

    Returns:
        A BuildHandler that creates (path, asgi_app) when called with options.
    """

    def build(default_options: _HandlerOptions) -> tuple[str, ASGIApp]:
        opts = _HandlerOptions(
            interceptors=list(default_options.interceptors),
            max_body_size=default_options.max_body_size,
        )
        for opt in options:
            opt(opts)

        app = asgi_app_factory(service_impl, interceptors=opts.interceptors)
        return app.path, app

    return build


def new_asgi_app(
    network_public_key: str,
    *build_handlers: BuildHandler,
) -> ASGIApp:
    """Create a composite ASGI app with signature verification.

    Go equivalent: NewHttpHandler(networkPublicKey, buildHandlers...)

    Args:
        network_public_key: Hex-encoded T-0 Network public key for signature verification.
            Pass empty string to disable signature verification.
        *build_handlers: Handler builders created via handler().

    Returns:
        An ASGI application with signature verification middleware.
    """
    default_options = _HandlerOptions(
        interceptors=[SignatureErrorInterceptor()],
    )

    # Build all service handlers
    routes: dict[str, ASGIApp] = {}
    for build in build_handlers:
        path, app = build(default_options)
        routes[path] = app

    # Create router ASGI app
    router = _create_router(routes)

    # Wrap with signature verification middleware if key provided
    if network_public_key:
        verify_fn = new_verify_signature(network_public_key)
        return signature_verification_middleware(router, verify_fn, default_options.max_body_size)

    return router


def _create_router(routes: dict[str, ASGIApp]) -> ASGIApp:
    """Create a simple path-prefix ASGI router.

    ConnectRPC requests have paths like:
    /tzero.v1.payment.ProviderService/PayOut

    The routes dict maps service path prefixes to their ASGI apps:
    {"/tzero.v1.payment.ProviderService": <app>}
    """

    async def router(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # Non-HTTP scopes (e.g. lifespan) - try first handler
            if routes:
                first_app = next(iter(routes.values()))
                await first_app(scope, receive, send)
            return

        path = scope.get("path", "")
        for prefix, app in routes.items():
            if path.startswith(prefix):
                await app(scope, receive, send)
                return

        # No matching route - return 404
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})

    return router
