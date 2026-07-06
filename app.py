"""Tangent FastAPI application / local launcher.

Inspired by tangle's ``start_local.py``. This module wires together the three
Tangent routers (opencode UI proxy, instance management, and the generic HTTP/WS
proxy), provides dummy single-user auth for local development, and demonstrates
how to build the mitmproxy "auth proxy" config from environment variables.

Run it with::

    ENV=development \\
    OPENAI_API_KEY=sk-... \\
    TANGENT_TANGLE_API_URL=tangle.tangleml.com TANGLE_AUTH_JWT_SECRET=... \\
        uvicorn app:app --reload

or simply ``python app.py``.
"""

import datetime
import getpass
import json
import logging
import os
import typing

import fastapi
import jwt
from kubernetes import client as k8s_client_lib
from kubernetes import config as k8s_config

from tangent import (
    instance_management_routes,
    opencode_routes,
    proxy_routes,
)

logger = logging.getLogger("tangent.app")

# --------------------------------------------------------------------------- #
# Environment / deployment configuration
# --------------------------------------------------------------------------- #
ENV = os.environ.get("ENV", "development")

KUBERNETES_NAMESPACE = os.environ.get("TANGENT_KUBERNETES_NAMESPACE", "default")
KUBERNETES_SERVICE_ACCOUNT_NAME = os.environ.get(
    "TANGENT_KUBERNETES_SERVICE_ACCOUNT_NAME"
) or None
GCS_BUCKET = os.environ.get("TANGENT_GCS_BUCKET", "tangent-data")

API_PREFIX = "/api/tangent"

# Where the pre-built OpenCode web UI static files live inside the container.
# TODO: Include the build files in the container.
OPENCODE_APP_BUILD_DIR = os.environ.get(
    "TANGENT_OPENCODE_APP_BUILD_DIR", "tangent/opencode_app/dist_files"
)

# --------------------------------------------------------------------------- #
# Authentication (dummy single-user auth for local development)
# --------------------------------------------------------------------------- #
def get_current_user(connection: fastapi.requests.HTTPConnection) -> str:
    """FastAPI dependency: return the authenticated user's name.

    This is a placeholder: in development it trusts the local OS user, and
    outside development it refuses every request.

    IMPORTANT: replace this with real authentication/authorization (OAuth,
    verified JWTs, an identity-aware proxy, ...) before running anywhere
    multi-user or public.

    We annotate the parameter as ``fastapi.requests.HTTPConnection`` rather than
    ``fastapi.Request`` on purpose: HTTPConnection is the shared base of both
    ``Request`` (HTTP) and ``WebSocket``, so FastAPI injects it for HTTP *and*
    WebSocket routes. A ``Request`` annotation is only injected for HTTP routes;
    on a WebSocket route it would be missing and 500 the upgrade handshake.
    """
    if ENV == "development":
        return f"{getpass.getuser()}@localhost"

    raise fastapi.HTTPException(
        status_code=fastapi.status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Authentication is not configured. Replace get_current_user with a"
            " real authentication dependency before deploying."
        ),
    )


# --------------------------------------------------------------------------- #
# AI provider credentials (injected into agent egress by the auth proxy)
# --------------------------------------------------------------------------- #
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# Optional: rewrite api.openai.com/v1 -> a compatible gateway (e.g. a LiteLLM URL).
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL")


# =========================================================================== #
# ⚙️  CONFIGURE ME: connecting Tangent instances to your Tangle API
# =========================================================================== #
# Tangent instances talk to Tangle through the auth proxy, which injects auth
# headers on requests going to ``TANGLE_API_URL``. Configure the auth headers
# with these env vars (combine as many as you need):
#
#   TANGLE_API_URL                          host (+ path prefix) of the Tangle API.
#   TANGLE_AUTH_HEADERS                      static headers as a JSON object, e.g.
#                                            '{"X-Api-Key": "..."}'.
#   TANGLE_AUTH_USE_BASIC                    -> Authorization: Basic <value>.
#   TANGLE_AUTH_USE_JWT_BEARER=true          -> Authorization: Bearer <minted JWT>.
#   TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME=X   -> Cookie: X=<minted JWT>.
#   TANGLE_AUTH_JWT_SECRET                   HS256 secret for the minted JWT.
#   TANGLE_AUTH_JWT_ISSUER / _AUDIENCE       JWT `iss` / `aud` claims.
#   TANGLE_AUTH_JWT_EXPIRATION_SECONDS       JWT lifetime (`exp`); unset = no exp.
#
# Bearer JWT auth is the default: setting only TANGLE_AUTH_JWT_SECRET (with no
# other method chosen) enables it. Set TANGLE_AUTH_USE_JWT_BEARER=false to opt
# out. TANGLE_AUTH_USE_BASIC and TANGLE_AUTH_USE_JWT_BEARER are mutually
# exclusive (both set the Authorization header); everything else may be combined.
# If no auth is configured, a warning is emitted at startup and instances are
# created *without* Tangle credentials — they won't be able to reach Tangle.
# =========================================================================== #
TANGLE_API_URL = os.environ.get("TANGENT_TANGLE_API_URL")  # e.g. "tangle.tangleml.com"

# HS256 secret and claims used to sign the JWT for bearer / cookie auth.
TANGLE_AUTH_JWT_SECRET = os.environ.get("TANGLE_AUTH_JWT_SECRET")
TANGLE_AUTH_JWT_ISSUER = os.environ.get("TANGLE_AUTH_JWT_ISSUER", "tangent.tangleml.com")
TANGLE_AUTH_JWT_AUDIENCE = os.environ.get(
    "TANGLE_AUTH_JWT_AUDIENCE", "tangle.tangleml.com"
)
# JWT lifetime in seconds; when unset, the token is minted without an `exp` claim.
_tangle_jwt_expiration_env = os.environ.get("TANGLE_AUTH_JWT_EXPIRATION_SECONDS")
TANGLE_AUTH_JWT_EXPIRATION_SECONDS = (
    int(_tangle_jwt_expiration_env) if _tangle_jwt_expiration_env else None
)

# Static headers to always inject, as a JSON object.
TANGLE_AUTH_HEADERS = os.environ.get("TANGLE_AUTH_HEADERS")
# Pre-encoded basic credentials -> `Authorization: Basic <value>`.
TANGLE_AUTH_USE_BASIC = os.environ.get("TANGLE_AUTH_USE_BASIC")
# Mint a JWT and send it as `Cookie: <name>=<jwt>`.
TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME = os.environ.get(
    "TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME"
)
# Mint a JWT and send it as `Authorization: Bearer <jwt>`. Defaults to on when a
# JWT secret is set and no other method was chosen; set it explicitly to override.
_tangle_bearer_env = os.environ.get("TANGLE_AUTH_USE_JWT_BEARER")
if _tangle_bearer_env is not None:
    TANGLE_AUTH_USE_JWT_BEARER = _tangle_bearer_env.lower() in ("1", "true", "yes")
else:
    TANGLE_AUTH_USE_JWT_BEARER = bool(
        TANGLE_AUTH_JWT_SECRET
        and not TANGLE_AUTH_USE_BASIC
        and not TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME
    )

if TANGLE_AUTH_USE_BASIC and TANGLE_AUTH_USE_JWT_BEARER:
    raise ValueError(
        "TANGLE_AUTH_USE_BASIC cannot be combined with TANGLE_AUTH_USE_JWT_BEARER:"
        " both set the Authorization header."
    )


def _mint_tangle_jwt(user_name: str) -> str:
    """Sign a short-lived Tangle auth JWT for ``user_name``."""
    if not TANGLE_AUTH_JWT_SECRET:
        raise ValueError(
            "TANGLE_AUTH_JWT_SECRET must be set to use TANGLE_AUTH_USE_JWT_BEARER or"
            " TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME."
        )
    issued_at = datetime.datetime.now(tz=datetime.UTC)
    payload = {
        "email": user_name,
        "sub": user_name,
        "iss": TANGLE_AUTH_JWT_ISSUER,
        "aud": TANGLE_AUTH_JWT_AUDIENCE,
        "iat": issued_at,
    }
    if TANGLE_AUTH_JWT_EXPIRATION_SECONDS is not None:
        payload["exp"] = issued_at + datetime.timedelta(
            seconds=TANGLE_AUTH_JWT_EXPIRATION_SECONDS
        )
    return jwt.encode(payload=payload, key=TANGLE_AUTH_JWT_SECRET, algorithm="HS256")


def generate_tangle_auth_headers(user_name: str) -> dict[str, str] | None:
    """USER-CONFIGURABLE: headers that authenticate ``user_name`` to Tangle.

    Merges whichever of the ``TANGLE_AUTH_*`` env vars are set (see the block
    above). Returns ``None`` when nothing is configured. Replace the body if your
    Tangle expects something else entirely.
    """
    headers: dict[str, str] = {}

    if TANGLE_AUTH_HEADERS:
        headers.update(json.loads(TANGLE_AUTH_HEADERS))

    if TANGLE_AUTH_USE_BASIC:
        headers["Authorization"] = f"Basic {TANGLE_AUTH_USE_BASIC}"

    if TANGLE_AUTH_USE_JWT_BEARER or TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME:
        user_auth_jwt = _mint_tangle_jwt(user_name)
        if TANGLE_AUTH_USE_JWT_BEARER:
            headers["Authorization"] = f"Bearer {user_auth_jwt}"
        if TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME:
            headers["Cookie"] = f"{TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME}={user_auth_jwt}"

    return headers or None


# --------------------------------------------------------------------------- #
# Auth proxy configuration
# --------------------------------------------------------------------------- #
def _build_proxy_config(user_name: str) -> dict:
    """Render the mitmproxy rule config that injects auth headers on egress.

    Each rule matches outgoing requests whose URL starts with ``url_pattern`` and
    then (optionally) rewrites the URL via ``replacement_pattern`` and adds the
    given ``add_headers``. See ``instance_management._AUTH_PROXY_MITMPROXY_ADDON_PY``
    for the matching logic that runs inside the instance's proxy sidecar.
    """
    rules: list[dict] = []

    if OPENAI_API_KEY:
        openai_rule: dict = {
            "url_pattern": "api.openai.com/v1",
            "add_headers": {"Authorization": f"Bearer {OPENAI_API_KEY}"},
        }
        if OPENAI_BASE_URL:
            openai_rule["replacement_pattern"] = OPENAI_BASE_URL
        rules.append(openai_rule)

    if ANTHROPIC_API_KEY:
        anthropic_rule: dict = {
            "url_pattern": "api.anthropic.com/v1",
            "add_headers": {
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        }
        if ANTHROPIC_BASE_URL:
            anthropic_rule["replacement_pattern"] = ANTHROPIC_BASE_URL
        rules.append(anthropic_rule)

    if TANGLE_API_URL:
        tangle_auth_headers = generate_tangle_auth_headers(user_name)
        if tangle_auth_headers:
            rules.append(
                {
                    "url_pattern": TANGLE_API_URL,
                    "add_headers": tangle_auth_headers,
                }
            )

    return {"rules": rules}


def generate_proxy_config(
    user_name: typing.Annotated[str, fastapi.Depends(get_current_user)],
) -> dict:
    """FastAPI dependency: the auth-proxy config for the current user.

    Resolved per instance-creation request so the Tangle credentials are minted
    for the user that owns the new instance.
    """
    return _build_proxy_config(user_name=user_name)


# --------------------------------------------------------------------------- #
# Kubernetes client
# --------------------------------------------------------------------------- #
def _load_kubernetes_client() -> k8s_client_lib.ApiClient:
    """Load in-cluster config when running inside a Pod, else the local kubeconfig."""
    try:
        k8s_config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config.")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        logger.info("Loaded local kubeconfig.")
    return k8s_client_lib.ApiClient()


# --------------------------------------------------------------------------- #
# Startup checks
# --------------------------------------------------------------------------- #
def _warn_about_configuration() -> None:
    if not (TANGLE_API_URL and generate_tangle_auth_headers("startup-check")):
        logger.warning(
            "Tangle is not configured: set TANGENT_TANGLE_API_URL and at least one"
            " TANGLE_AUTH_* variable (or customize generate_tangle_auth_headers)."
            " Tangent instances will be created WITHOUT Tangle credentials and won't"
            " be able to connect to Tangle."
        )
    if not (OPENAI_API_KEY or ANTHROPIC_API_KEY):
        logger.warning(
            "No AI provider configured: set OPENAI_API_KEY and/or ANTHROPIC_API_KEY so"
            " the auth proxy can authenticate agent model calls."
        )
    if ENV == "development":
        logger.warning(
            "ENV=development: using dummy single-user auth (get_current_user)."
            " Do not use this in a shared or public deployment."
        )


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #
def create_app() -> fastapi.FastAPI:
    logging.basicConfig(level=logging.INFO)

    # The `httpx` library logs every HTTP request at INFO level. Suppress it.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _warn_about_configuration()

    kubernetes_client = _load_kubernetes_client()

    app = fastapi.FastAPI(title="Tangent", version="0.1.0")

    @app.get("/ping", tags=["health"])
    def ping() -> dict:
        return {"status": "ok"}

    app.include_router(
        opencode_routes.build_api_router(
            kubernetes_client=kubernetes_client,
            kubernetes_namespace=KUBERNETES_NAMESPACE,
            opencode_app_build_dir=OPENCODE_APP_BUILD_DIR,
            get_user_name=get_current_user,
        ),
        prefix=API_PREFIX,
    )

    app.include_router(
        instance_management_routes.build_api_router(
            api_prefix=API_PREFIX,
            get_user_name=get_current_user,
            generate_proxy_config=generate_proxy_config,
            gcs_bucket=GCS_BUCKET,
            kubernetes_client=kubernetes_client,
            kubernetes_namespace=KUBERNETES_NAMESPACE,
            kubernetes_service_account_name=KUBERNETES_SERVICE_ACCOUNT_NAME,
        ),
    )

    app.include_router(
        proxy_routes.build_api_router(
            api_prefix=API_PREFIX,
            kubernetes_client=kubernetes_client,
            kubernetes_namespace=KUBERNETES_NAMESPACE,
            get_user_id=get_current_user,
        )
    )

    return app


app = create_app()


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover
        raise SystemExit(
            "uvicorn is required to run the app directly: `uv add uvicorn` or"
            " `pip install uvicorn`. Alternatively serve with `fastapi dev app.py`."
        ) from error

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=ENV == "development",
    )
