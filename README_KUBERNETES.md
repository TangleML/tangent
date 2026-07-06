# Tangent - AI Agent hosting for Tangle

## Tangent-on-Kubernetes backend app blueprint

[`app.py`](app.py) wires the Tangent routers (instance
management, and the generic HTTP/WebSocket proxy) into a single FastAPI app,
provides dummy single-user auth for local development, and shows how to build
the mitmproxy "auth proxy" config that injects credentials into agent egress.

> [!IMPORTANT]
> **`app.py` is a starting blueprint, not a complete, production-ready
> solution.** In particular, its authentication is a placeholder. Before you run
> Tangent anywhere multi-user or reachable by others, you **must** replace the
> `get_current_user` routine with real authentication (see
> [Overriding authentication](#overriding-authentication) below). Everything
> else — the Tangle credentials, the proxy rules — is meant to be reviewed and
> adapted to your environment too.

### Running it

Install dependencies and start the server:

```bash
uv sync
ENV=development uv run uvicorn app:app --reload
```

You can also run it directly (`python app.py`) or with `fastapi dev app.py`.
When `ENV=development`, the server auto-reloads and uses the dummy auth below.

Once running:

- API docs: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/ping>
- Create/list instances: `GET`/`POST` `/api/tangent/instances`

`HOST` and `PORT` (default `0.0.0.0:8000`) control the bind address.

### Overriding authentication

The app identifies the caller through the `get_current_user` FastAPI dependency
in [`app.py`](app.py). The default implementation is a **placeholder for local
development only**: when `ENV=development` it trusts the local OS user
(`<you>@localhost`); otherwise it rejects every request with `401`.

This dependency is injected into every router (instance management, the proxies,
and the per-user Tangle credential minting), so it is the single point that
decides "who is this request from". **You must replace it** with real
authentication — e.g. verifying an OAuth session, a signed JWT, or a header set
by an identity-aware proxy in front of Tangent:

```python
def get_current_user(connection: fastapi.requests.HTTPConnection) -> str:
    token = connection.headers.get("Authorization")
    ...  # verify the token / session and return the authenticated user id
```

It is annotated with `fastapi.requests.HTTPConnection` (not `fastapi.Request`)
on purpose, so the same dependency works for both HTTP and WebSocket routes.

### Configuration

All configuration is via environment variables.

**Runtime / infrastructure**

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENV` | `development` | `development` enables dummy auth + auto-reload. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address. |
| `TANGENT_KUBERNETES_NAMESPACE` | `default` | Namespace instances are created in. |
| `TANGENT_KUBERNETES_SERVICE_ACCOUNT_NAME` | _(none)_ | Service account for instance pods. |
| `TANGENT_GCS_BUCKET` | `tangent-data` | GCS bucket for instance data. |
| `TANGENT_OPENCODE_APP_BUILD_DIR` | `tangent/opencode_app/dist_files` | OpenCode web UI static files. |

The Kubernetes client loads in-cluster config when running inside a pod, and
falls back to your local kubeconfig otherwise.

**AI provider credentials** (injected by the auth proxy into agent egress)

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Adds `Authorization: Bearer …` to `api.openai.com/v1` requests. |
| `OPENAI_BASE_URL` | Optional: rewrite `api.openai.com/v1` to a compatible gateway. |
| `ANTHROPIC_API_KEY` | Adds `x-api-key` / `anthropic-version` to `api.anthropic.com/v1`. |
| `ANTHROPIC_BASE_URL` | Optional: rewrite `api.anthropic.com/v1`. |

**Connecting instances to the Tangle API**

Set the API host and at least one auth method. If unconfigured, a startup
warning is emitted and instances are created *without* Tangle credentials.

| Variable | Purpose |
| --- | --- |
| `TANGENT_TANGLE_API_URL` | Host (+ path prefix) of the Tangle API, e.g. `tangle.tangleml.com`. |
| `TANGLE_AUTH_HEADERS` | Static headers to inject, as a JSON object, e.g. `{"X-Api-Key": "…"}`. |
| `TANGLE_AUTH_USE_BASIC` | `Authorization: Basic <value>`. |
| `TANGLE_AUTH_USE_JWT_BEARER` | `Authorization: Bearer <minted JWT>`. **On by default** when a JWT secret is set and no other method is chosen; set `=false` to opt out. |
| `TANGLE_AUTH_USE_JWT_COOKIE_WITH_NAME` | `Cookie: <name>=<minted JWT>`. |
| `TANGLE_AUTH_JWT_SECRET` | HS256 secret used to sign the minted JWT. |
| `TANGLE_AUTH_JWT_ISSUER` | JWT `iss` claim (default `tangent.tangleml.com`). |
| `TANGLE_AUTH_JWT_AUDIENCE` | JWT `aud` claim (default `tangle.tangleml.com`). |
| `TANGLE_AUTH_JWT_EXPIRATION_SECONDS` | JWT lifetime (`exp`); unset means no expiry. |

These methods can be combined (e.g. a static header plus a JWT cookie), except
that `TANGLE_AUTH_USE_BASIC` and `TANGLE_AUTH_USE_JWT_BEARER` are mutually
exclusive since both set the `Authorization` header. For anything beyond these
schemes, edit the `generate_tangle_auth_headers` function in [`app.py`](app.py)
directly.
