"""Build and create the Kubernetes resources that make up a Tangent agent instance."""

from __future__ import annotations

import dataclasses
import re
import textwrap
import yaml

from kubernetes import client as k8s_client_lib

DEFAULT_NAMESPACE = "default"
DEFAULT_SERVICE_ACCOUNT_NAME = None
DEFAULT_MITMPROXY_IMAGE = "mitmproxy/mitmproxy:12.2.2"
DEFAULT_AGENT_PORT = 8000
DEFAULT_PROXY_PORT = 8080
DEFAULT_PVC_SIZE = "1Gi"

# Secret keys
_PROXY_CONFIG_KEY = "auth_proxy_config.yaml"

DEFAULT_AGENT_KIND = "opencode"


def make_resource_name(instance_id: str) -> str:
    return f"tangent-{instance_id}"


def _sanitize_kubernetes_label_value(value: str) -> str:
    """Coerce an arbitrary string into a valid Kubernetes label value.

    Label values must be <=63 chars, begin and end with [a-z0-9A-Z], and only
    contain alphanumerics, dashes, underscores, and dots in between.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", value)[:63]
    return re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", sanitized)


def _build_secret(
    instance_id: str,
    proxy_config: dict,
    namespace: str = DEFAULT_NAMESPACE,
) -> k8s_client_lib.V1Secret:
    """Secret holding the proxy rule config and the Tangle CLI basic auth."""
    name = make_resource_name(instance_id)
    return k8s_client_lib.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client_lib.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={"tangent.tangleml.com/instance.id": instance_id},
        ),
        type="Opaque",
        string_data={
            _PROXY_CONFIG_KEY: yaml.safe_dump(proxy_config, sort_keys=False),
        },
    )


# The mitmproxy addon script that reads the rule file and rewrites headers.
# Same logic as the YAML — kept here so the whole behavior lives in one module.
_AUTH_PROXY_MITMPROXY_ADDON_PY = textwrap.dedent("""\
    import os

    from mitmproxy import http
    import yaml

    config_path = os.environ["PROXY_CONFIG_PATH"]
    with open(config_path, "r") as reader:
        proxy_config: dict = yaml.safe_load(reader)

    proxy_rules: list[dict] = proxy_config.get("rules") or []


    class AddHeaders:
        def request(self, flow: http.HTTPFlow):
            url = flow.request.pretty_url
            for rule in proxy_rules:
                url_pattern = rule["url_pattern"]
                replacement_pattern = rule.get("replacement_pattern")
                if (
                    url.startswith(url_pattern)
                    or url.startswith("https://" + url_pattern)
                    or url.startswith("http://" + url_pattern)
                ):
                    if replacement_pattern:
                        flow.request.url = flow.request.url.replace(url_pattern, replacement_pattern)
                    for header_key, header_value in (rule.get("add_headers") or {}).items():
                        flow.request.headers[header_key] = header_value
            flow.request.headers["x-tangent-proxy"] = "true"

        def response(self, flow: http.HTTPFlow):
            if flow.response:
                flow.response.headers["x-tangent-proxy"] = "true"


    addons = [AddHeaders()]
    """)


def _build_proxy_container(image: str, port: int) -> k8s_client_lib.V1Container:
    bootstrap = textwrap.dedent(f"""\
        python3 -m pip install PyYaml
        program_path=$(mktemp)
        printf "%s" "$0" > "$program_path"
        PROXY_CONFIG_PATH=/tangent/proxy-config/auth_proxy_config.yaml \\
            mitmdump -p {port} --script "$program_path"
        """)
    return k8s_client_lib.V1Container(
        name="tangle-proxy",
        image=image,
        command=["sh", "-ec", bootstrap, _AUTH_PROXY_MITMPROXY_ADDON_PY],
        volume_mounts=[
            k8s_client_lib.V1VolumeMount(
                name="proxy-config",
                mount_path="/tangent/proxy-config",
            ),
            k8s_client_lib.V1VolumeMount(
                name="proxy-ca-cert",
                mount_path="/root/.mitmproxy",
            ),
        ],
    )


def _build_mitmproxy_ca_cert_init_container(image: str) -> k8s_client_lib.V1Container:
    # `mitmdump --no-server --rfile /dev/null` exits immediately after writing
    # the CA cert at ~/.mitmproxy/mitmproxy-ca-cert.pem, which we then share
    # with the agent container via the proxy-ca-cert emptyDir.
    return k8s_client_lib.V1Container(
        name="proxy-ca-cert-generator",
        image=image,
        command=["sh", "-ec", "mitmdump --no-server --rfile /dev/null"],
        volume_mounts=[
            k8s_client_lib.V1VolumeMount(
                name="proxy-ca-cert",
                mount_path="/root/.mitmproxy",
            ),
        ],
    )


def _build_volumes(secret_name: str, bucket_name: str) -> list[k8s_client_lib.V1Volume]:
    return [
        k8s_client_lib.V1Volume(
            name="tangent-data",
            csi=k8s_client_lib.V1CSIVolumeSource(
                driver="gcsfuse.csi.storage.gke.io",
                volume_attributes={
                    "bucketName": bucket_name,
                    "mountOptions": "implicit-dirs",
                },
            ),
        ),
        k8s_client_lib.V1Volume(
            name="proxy-config",
            secret=k8s_client_lib.V1SecretVolumeSource(secret_name=secret_name),
        ),
        k8s_client_lib.V1Volume(
            name="proxy-ca-cert",
            empty_dir=k8s_client_lib.V1EmptyDirVolumeSource(),
        ),
    ]


def _build_stateful_set(
    instance_id: str,
    created_by: str,
    agent_kind: str,
    gcs_bucket: str,
    namespace: str = DEFAULT_NAMESPACE,
    service_account_name: str | None = DEFAULT_SERVICE_ACCOUNT_NAME,
    mitmproxy_image: str = DEFAULT_MITMPROXY_IMAGE,
    pvc_size: str = DEFAULT_PVC_SIZE,
) -> k8s_client_lib.V1StatefulSet:
    name = make_resource_name(instance_id)

    selector_labels = {"app": name}
    created_by_label = _sanitize_kubernetes_label_value(created_by.replace("@", "-at-"))

    resource_labels = {
        **selector_labels,
        "tangent.tangleml.com": "true",
        "tangent.tangleml.com/instance.id": instance_id,
        "tangent.tangleml.com/instance.created_by.sanitized": created_by_label,
    }
    resource_annotations = {
        "tangent.tangleml.com": "true",
        "tangent.tangleml.com/instance.id": instance_id,
        "tangent.tangleml.com/instance.created_by": created_by,
    }
    pod_annotations = {
        **resource_annotations,
        "tangent.tangleml.com/instance.agent": agent_kind,
        "gke-gcsfuse/volumes": "true",
    }

    if agent_kind == "opencode":
        agent_container = _build_opencode_agent_container(instance_id=instance_id)
    else:
        raise ValueError(f"Unsupported agent kind {agent_kind}")

    pod_spec = k8s_client_lib.V1PodSpec(
        service_account_name=service_account_name,
        init_containers=[_build_mitmproxy_ca_cert_init_container(mitmproxy_image)],
        containers=[
            agent_container,
            _build_proxy_container(image=mitmproxy_image, port=DEFAULT_PROXY_PORT),
        ],
        volumes=_build_volumes(secret_name=name, bucket_name=gcs_bucket),
    )

    pod_template = k8s_client_lib.V1PodTemplateSpec(
        metadata=k8s_client_lib.V1ObjectMeta(
            labels=resource_labels, annotations=pod_annotations
        ),
        spec=pod_spec,
    )

    pvc = k8s_client_lib.V1PersistentVolumeClaim(
        metadata=k8s_client_lib.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=resource_labels,
            annotations=resource_annotations,
        ),
        spec=k8s_client_lib.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=k8s_client_lib.V1VolumeResourceRequirements(
                requests={"storage": pvc_size},
            ),
        ),
    )

    stateful_set_spec = k8s_client_lib.V1StatefulSetSpec(
        service_name=name,
        replicas=1,
        selector=k8s_client_lib.V1LabelSelector(match_labels=selector_labels),
        template=pod_template,
        volume_claim_templates=[pvc],
    )

    return k8s_client_lib.V1StatefulSet(
        api_version="apps/v1",
        kind="StatefulSet",
        metadata=k8s_client_lib.V1ObjectMeta(name=name, namespace=namespace),
        spec=stateful_set_spec,
    )


def _generate_random_id() -> str:
    import os
    import time

    random_bytes = os.urandom(4)
    nanoseconds = time.time_ns()
    milliseconds = nanoseconds // 1_000_000

    return ("%012x" % milliseconds) + random_bytes.hex()


@dataclasses.dataclass(kw_only=True)
class TangentInstance:
    instance_id: str
    agent_kinds: list[str] | None = None
    # kubernetes_namespace: str
    # kubernetes_resource_name: str


def create_instance(
    *,
    api_client: k8s_client_lib.ApiClient,
    created_by: str,
    agent_kind: str,
    gcs_bucket: str,
    proxy_config: dict,
    namespace: str = DEFAULT_NAMESPACE,
    service_account_name: str | None = DEFAULT_SERVICE_ACCOUNT_NAME,
    mitmproxy_image: str = DEFAULT_MITMPROXY_IMAGE,
    pvc_size: str = DEFAULT_PVC_SIZE,
) -> TangentInstance:
    """Create the Secret and StatefulSet for a new agent instance.

    The Secret is created before the StatefulSet so the proxy-config volume
    mount succeeds on first pod start.
    """
    instance_id = _generate_random_id()

    secret = _build_secret(
        instance_id=instance_id,
        namespace=namespace,
        proxy_config=proxy_config,
    )
    stateful_set = _build_stateful_set(
        instance_id=instance_id,
        created_by=created_by,
        namespace=namespace,
        service_account_name=service_account_name,
        mitmproxy_image=mitmproxy_image,
        gcs_bucket=gcs_bucket,
        agent_kind=agent_kind,
        pvc_size=pvc_size,
    )

    core_v1 = k8s_client_lib.CoreV1Api(api_client=api_client)
    apps_v1 = k8s_client_lib.AppsV1Api(api_client=api_client)

    core_v1.create_namespaced_secret(namespace=namespace, body=secret)
    apps_v1.create_namespaced_stateful_set(namespace=namespace, body=stateful_set)

    result = TangentInstance(
        instance_id=instance_id,
        agent_kinds=[agent_kind],
        # kubernetes_namespace=namespace,
        # kubernetes_resource_name=stateful_set.metadata.name,
    )
    return result


def list_instances(
    *,
    api_client: k8s_client_lib.ApiClient,
    created_by: str | None = None,
    namespace: str | None = None,
):
    core_v1 = k8s_client_lib.CoreV1Api(api_client=api_client)
    label_selector = "tangent.tangleml.com=true"
    if created_by:
        created_by_label = _sanitize_kubernetes_label_value(
            created_by.replace("@", "-at-")
        )
        label_selector = (
            label_selector
            + f",tangent.tangleml.com/instance.created_by.sanitized={created_by_label}"
        )
    if namespace:
        pod_list = core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        )
    else:
        pod_list = core_v1.list_pod_for_all_namespaces(label_selector=label_selector)

    instances = []
    for pod in pod_list.items:
        instance_id = pod.metadata.labels["tangent.tangleml.com/instance.id"]
        instance = TangentInstance(
            instance_id=instance_id,
            # kubernetes_namespace=pod.metadata.namespace,
            # # TODO: Improve
            # kubernetes_resource_name=pod.metadata.name.removesuffix("-0"),
        )
        instances.append(instance)
    return instances


# region OpenCode agent
DEFAULT_OPENCODE_AGENT_IMAGE = "ghcr.io/anomalyco/opencode@sha256:92ed7f558889354730373df7da7e59bdb985a37b74e01ccd6b7909b98ad5290e"


def _build_opencode_agent_container_command_script() -> str:
    """The startup script for the agent container."""
    return textwrap.dedent("""\
        # Install proxy CA certificates
        # apk --no-cache add ca-certificates
        # update-ca-certificates
        # Installing certificate manually
        cat /usr/local/share/ca-certificates/* >> /etc/ssl/certs/ca-certificates.crt
        # ! Python're request library ignores the system's CA certificate bundle and uses a built-in bundle. See https://stackoverflow.com/a/42982144/1497385
        # Error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
        # Fixing that by overriding the CA bundle.
        export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

        # Install uv
        apk --no-cache add curl
        curl -LsSf https://astral.sh/uv/install.sh | sh

        export PATH="$PATH:$HOME/.local/bin"


        # [Not] Installing Google Cloud SDK in background
        apk add --update python3 bash
        export PATH="$PATH:/root/google-cloud-sdk/bin/"
        # curl -sSL https://sdk.cloud.google.com | bash 2>/dev/null && /root/google-cloud-sdk/bin/gcloud auth list && /root/google-cloud-sdk/bin/gcloud config set core/custom_ca_certs_file /etc/ssl/certs/ca-certificates.crt &

        # Activating proxy (The `http_*` casing matters for curl). https://superuser.com/questions/876100/https-proxy-vs-https-proxy
        # gcloud CLI only supports HTTP proxies https://docs.cloud.google.com/sdk/docs/proxy-settings#proxy_configuration
        # export HTTPS_PROXY=https://localhost:8080
        export HTTPS_PROXY=http://localhost:8080
        export http_proxy=http://localhost:8080

        # Copy OpenCode skills and providers config
        # cp -rf /tangent/agent_config/.config/opencode/skills/ ~/.config/opencode/skills/
        # cp -f /tangent/agent_config/.config/opencode/opencode.json ~/.config/opencode/opencode.json
        # cp -f /tangent/agent_config/.local/share/opencode/auth.json ~/.local/share/opencode/auth.json
        # Note: On MacOS, having a trailing slash at the end of the source path ensures that child items (not directory itself) are copied to the destination directory regardless of its existence.
        # But on Linux cp works differently. On Linux in most cases asterisk works: `cp -r a/* b/`. However asterisk expansion skips "hidden" files/directories like ".config".
        # Using trailing `/.` can help: `cp -r a/. b/`. Best solution: `--no-target-directory` or `-T`.
        cp --archive --recursive --force --no-target-directory /tangent/agent_config/ ~/

        # Run OpenCode
        mkdir -p ~/workspace
        cd ~/workspace
        export OPENCODE_DISABLE_AUTOUPDATE=1
        # Without --hostname 0.0.0.0 the GKE Console proxy can access teh service but the Kubernetes API Server Proxy API cannot.
        opencode serve --port 8000 --hostname 0.0.0.0
        """)


def _build_opencode_agent_container(
    instance_id: str,
    image: str = DEFAULT_OPENCODE_AGENT_IMAGE,
) -> k8s_client_lib.V1Container:
    env = [
        k8s_client_lib.V1EnvVar(
            name="TANGENT_INSTANCE_ID",
            value_from=k8s_client_lib.V1EnvVarSource(
                field_ref=k8s_client_lib.V1ObjectFieldSelector(
                    field_path="metadata.labels['tangent.tangleml.com/instance.id']",
                ),
            ),
        ),
    ]

    pvc_volume_name = make_resource_name(instance_id)

    volume_mounts = [
        k8s_client_lib.V1VolumeMount(
            name="tangent-data",
            mount_path="/tangent/packages_to_install",
            sub_path="agent_configs/packages_to_install",
            read_only=True,
        ),
        # Persisting sessions for OpenCode
        k8s_client_lib.V1VolumeMount(
            name="tangent-data",
            mount_path="/root/.local/share/opencode",
            sub_path_expr="user_data/instances/$(TANGENT_INSTANCE_ID)/opencode/.local/share/opencode",
        ),
        k8s_client_lib.V1VolumeMount(
            name="tangent-data",
            mount_path="/tangent/agent_config",
            sub_path="agent_configs/opencode",
            read_only=True,
        ),
        k8s_client_lib.V1VolumeMount(
            name="tangent-data",
            mount_path="/root/workspace/memory.read_only/",
            sub_path_expr="user_data/memory/by_instance/",
            read_only=True,
        ),
        k8s_client_lib.V1VolumeMount(
            name="tangent-data",
            mount_path="/root/workspace/memory/",
            sub_path_expr="user_data/memory/by_instance/$(TANGENT_INSTANCE_ID)/",
        ),
        k8s_client_lib.V1VolumeMount(
            name="proxy-ca-cert",
            mount_path="/usr/local/share/ca-certificates/mitmproxy.crt",
            sub_path="mitmproxy-ca-cert.pem",
            read_only=True,
        ),
        k8s_client_lib.V1VolumeMount(
            name=pvc_volume_name,
            mount_path="/root/workspace",
            sub_path="root/workspace",
        ),
    ]

    return k8s_client_lib.V1Container(
        name="agent-opencode",
        image=image,
        env=env,
        command=["sh", "-xc", _build_opencode_agent_container_command_script()],
        volume_mounts=volume_mounts,
    )


# endregion
