"""Reverse proxy that forwards HTTP and WebSocket traffic to a Pod port through the
Kubernetes API-server Pod proxy subresource."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ssl

import httpx
import websockets
from kubernetes import client as k8s_client_lib
from kubernetes import config as k8s_config_lib
from kubernetes.client import configuration as k8s_client_config_lib
from kubernetes.stream import portforward as k8s_portforward
from starlette import background, requests, responses
from starlette import websockets as starlette_websockets
from websockets import exceptions as ws_exceptions


@dataclasses.dataclass
class KubernetesApiServerInfo:
    """Everything needed to send authenticated requests to the API server."""

    def __init__(
        self,
        kubernetes_configuration: k8s_client_config_lib.Configuration | None = None,
    ):
        if not kubernetes_configuration:
            try:
                k8s_config_lib.load_incluster_config()
            except k8s_config_lib.ConfigException:
                k8s_config_lib.load_kube_config()
            kubernetes_configuration = (
                k8s_client_config_lib.Configuration.get_default_copy()
            )
        assert kubernetes_configuration
        self._kubernetes_configuration = kubernetes_configuration

        # e.g. "https://127.0.0.1:6443" (no trailing slash)
        self.host = kubernetes_configuration.host.rstrip("/")

        ssl_context: ssl.SSLContext | None = None
        if self.host.startswith("https"):
            ssl_context = ssl.create_default_context(
                cafile=kubernetes_configuration.ssl_ca_cert or None
            )
            if not kubernetes_configuration.verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            if (
                kubernetes_configuration.cert_file
            ):  # client-certificate auth (e.g. docker-desktop, minikube)
                ssl_context.load_cert_chain(
                    certfile=kubernetes_configuration.cert_file,
                    keyfile=kubernetes_configuration.key_file,
                )
        self.ssl_context = ssl_context

    def get_auth_headers(self) -> dict[str, str]:
        # auth_settings calls get_api_key_with_prefix which triggers refresh_api_key_hook (if set by load_kube_config)
        # which handles token expiry for exec-based providers (gke-gcloud-auth-plugin, OIDC, etc.)
        auth_settings = self._kubernetes_configuration.auth_settings()
        bearer_auth_info = auth_settings.get("BearerToken")
        if bearer_auth_info:
            # Usually, bearer_auth_info["key"] == "authorization"
            return {bearer_auth_info["key"]: bearer_auth_info["value"]}
        else:
            raise ValueError(
                f"No Kubernetes auth info: {self._kubernetes_configuration=}"
            )


def _make_kubernetes_pod_proxy_uri_path(
    namespace: str, pod: str, port: int, path: str
) -> str:
    return f"/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy/{path.lstrip('/')}"


def _make_kubernetes_pod_proxy_http_url(
    api_server_host: str, namespace: str, pod: str, port: int, path: str
) -> str:
    return f"{api_server_host}{_make_kubernetes_pod_proxy_uri_path(namespace, pod, port, path)}"


def _make_kubernetes_pod_proxy_ws_url(
    api_server_host: str, namespace: str, pod: str, port: int, path: str
) -> str:
    if api_server_host.startswith("https://"):
        api_server_host = "wss://" + api_server_host.removeprefix("https://")
    elif api_server_host.startswith("http://"):
        api_server_host = "ws://" + api_server_host.removeprefix("http://")
    return f"{api_server_host}{_make_kubernetes_pod_proxy_uri_path(namespace, pod, port, path)}"


# Headers that are connection-specific and must not be forwarded verbatim.
_HOP_BY_HOP_HTTP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

_HTTP_METHODS_WITHOUT_BODY = frozenset({"GET", "HEAD", "OPTIONS"})


async def proxy_http(
    request: requests.Request,
    http_client: httpx.AsyncClient,
    kubernetes_server_info: KubernetesApiServerInfo,
    namespace: str,
    pod: str,
    port: int,
    path: str,
) -> responses.Response:
    """Forward a single HTTP request to the Pod and stream the response back."""
    url = _make_kubernetes_pod_proxy_http_url(
        api_server_host=kubernetes_server_info.host,
        namespace=namespace,
        pod=pod,
        port=port,
        path=path,
    )

    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP_HTTP_HEADERS
    }
    fwd_headers.update(
        kubernetes_server_info.get_auth_headers()
    )  # API-server auth wins

    content = (
        None
        if request.method.upper() in _HTTP_METHODS_WITHOUT_BODY
        else request.stream()
    )
    upstream_req = http_client.build_request(
        request.method,
        url,
        params=request.query_params,
        headers=fwd_headers,
        content=content,
    )
    try:
        upstream = await http_client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        return responses.Response(f"upstream error: {exc}", status_code=502)

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP_HTTP_HEADERS
    }
    return responses.StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=background.BackgroundTask(upstream.aclose),
    )


async def proxy_websocket(
    websocket: starlette_websockets.WebSocket,
    kubernetes_server_info: KubernetesApiServerInfo,
    namespace: str,
    pod: str,
    port: int,
    path: str,
) -> None:
    """Bridge a client WebSocket to the Pod's WebSocket through the API-server proxy."""
    url = _make_kubernetes_pod_proxy_ws_url(
        api_server_host=kubernetes_server_info.host,
        namespace=namespace,
        pod=pod,
        port=port,
        path=path,
    )
    subprotocols = websocket.scope.get("subprotocols") or None
    ssl_ctx = kubernetes_server_info.ssl_context if url.startswith("wss") else None

    try:
        upstream = await websockets.connect(
            url,
            ssl=ssl_ctx,
            additional_headers=kubernetes_server_info.get_auth_headers() or None,
            subprotocols=subprotocols,
            open_timeout=10,
            max_size=None,
            ping_interval=None,
        )
    except (OSError, asyncio.TimeoutError, ws_exceptions.WebSocketException):
        # Pod not ready yet, or upstream refused the upgrade. Reject the handshake;
        # the browser UI retries with backoff.
        await websocket.close(code=1011)
        return

    async with upstream:
        await websocket.accept(subprotocol=upstream.subprotocol)
        await _pipe_websockets_bidirectionally(websocket, upstream)


async def port_forward_websocket(
    websocket: starlette_websockets.WebSocket,
    api_client: k8s_client_lib.ApiClient,
    namespace: str,
    pod: str,
    port: int,
    path_and_query_string: str,
    scheme: str = "ws",
    host: str = "localhost",
) -> None:
    """Bridge a client WebSocket to the Pod's WebSocket.

    This routes through the API-server `portforward` subresource (a raw TCP
    tunnel) rather than the `pods/proxy` subresource. The proxy subresource
    strips the request query string when proxying a WebSocket upgrade, which
    breaks clients like socket.io that carry essential params (EIO, transport,
    sid) in the query string. portforward gives a transparent byte stream, so
    the upgrade request reaches the Pod unmodified.

    `scheme`/`host` only shape the upgrade request, not the transport (see the
    URI note below). They default to plaintext loopback, which is correct for
    every current agent type; they are exposed as parameters so a future agent
    that needs e.g. in-pod TLS (`wss`) or strict Host validation can override
    them without touching the proxy internals.
    """
    # The transport is the pre-connected `portforward` socket passed as `sock=`
    # below, so `websockets` never resolves or dials this URI. The URI only
    # supplies: the scheme (`ws` = plaintext, which is correct because the Pod
    # serves plain HTTP/WS on its container port and TLS terminates upstream at
    # the API server), the `Host:` header (`localhost:{port}` is a placeholder
    # authority matching how the agent, bound to 0.0.0.0, is reached in-pod),
    # and the request path. We preserve the original path + query so socket.io's
    # upgrade params survive.
    uri = f"{scheme}://{host}:{port}/{path_and_query_string.lstrip('/')}"
    subprotocols = websocket.scope.get("subprotocols") or None

    core_v1 = k8s_client_lib.CoreV1Api(api_client=api_client)

    # Establishing the portforward opens a (blocking) WebSocket to the API server
    # and spins up a pump thread; run it off the event loop.
    try:
        port_forward = await asyncio.to_thread(
            k8s_portforward,
            core_v1.connect_get_namespaced_pod_portforward,
            pod,
            namespace,
            ports=str(port),
        )
        upstream_socket = port_forward.socket(port)
        upstream_socket.setblocking(False)
    except Exception:
        # Pod not ready yet, or the portforward could not be established. Reject
        # the handshake; the browser UI retries with backoff.
        await websocket.close(code=1011)
        return

    try:
        upstream = await websockets.connect(
            uri,
            sock=upstream_socket,
            subprotocols=subprotocols,
            open_timeout=10,
            max_size=None,
            ping_interval=None,
        )
    except (OSError, asyncio.TimeoutError, ws_exceptions.WebSocketException):
        # port_forward.close() is blocking (joins the pump thread / closes the
        # API-server WebSocket), so it must run off the event loop.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(port_forward.close)
        await websocket.close(code=1011)
        return

    try:
        async with upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            await _pipe_websockets_bidirectionally(websocket, upstream)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(port_forward.close)


async def _pipe_websockets_bidirectionally(
    websocket: starlette_websockets.WebSocket, upstream: websockets.ClientConnection
) -> None:
    """Pump frames in both directions until either side closes."""

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
        except starlette_websockets.WebSocketDisconnect:
            pass
        finally:
            await upstream.close()

    async def upstream_to_client() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray)):
                    await websocket.send_bytes(bytes(frame))
                else:
                    await websocket.send_text(frame)
        except ws_exceptions.ConnectionClosed:
            pass
        finally:
            if (
                websocket.application_state
                != starlette_websockets.WebSocketState.DISCONNECTED
            ):
                with contextlib.suppress(
                    ws_exceptions.WebSocketException, RuntimeError
                ):
                    await websocket.close()

    await asyncio.gather(client_to_upstream(), upstream_to_client())
